"""Egasis API. The web process never starts the campaign worker."""
import csv
import hashlib
import hmac
import io
import html
import json
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from .models import Account, Campaign, Connection, Contact, Event, Job, Meeting, Message, Session, Suppression, Usage, User, Workspace
from .security import Vault, password_hash, token_hash, verify_password
from .settings import Settings
from .store import make_store

STATIC = Path(__file__).resolve().parents[1] / 'static'

def valid_email(value):
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', value):
        raise HTTPException(422, 'Ingresá un correo válido.')
    return value

def record(row, exclude=()):
    return {col.name: getattr(row, col.name) for col in row.__table__.columns if col.name not in exclude}

class Register(BaseModel):
    email: str
    password: str = Field(min_length=12, max_length=256)
    name: str = Field(min_length=2, max_length=160)
    invite: str = ''

class Login(BaseModel):
    email: str
    password: str = Field(max_length=256)

class CampaignInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    offer: str = Field(default='', max_length=5000)
    audience: str = Field(default='', max_length=2000)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=10, max_length=12000)
    daily_limit: int = Field(default=20, ge=1, le=500)
    start_hour: int = Field(default=9, ge=0, le=23)
    end_hour: int = Field(default=18, ge=1, le=24)
    weekdays: str = '0,1,2,3,4'
    reply_mode: str = 'review'
    auto_reply_body: str = Field(default='', max_length=12000)

class ContactInput(BaseModel):
    email: str
    name: str = Field(default='', max_length=160)
    company: str = Field(default='', max_length=200)
    website: str = Field(default='', max_length=2048)

class ContactsInput(BaseModel):
    contacts: list[ContactInput] = Field(max_length=1000)

class AccountInput(BaseModel):
    email: str
    display_name: str = Field(default='', max_length=160)
    password: str = Field(min_length=1, max_length=1000)
    smtp_host: str = 'smtp.gmail.com'
    smtp_port: int = Field(default=587, ge=1, le=65535)
    imap_host: str = 'imap.gmail.com'
    imap_port: int = Field(default=993, ge=1, le=65535)
    daily_limit: int = Field(default=30, ge=1, le=500)
    cooldown_seconds: int = Field(default=120, ge=30, le=86400)

class WorkspaceInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    offer: str = Field(default='', max_length=5000)
    audience: str = Field(default='', max_length=2000)
    signature: str = Field(default='', max_length=1000)
    timezone: str = 'America/Argentina/Buenos_Aires'
    daily_budget: float = Field(default=5, gt=0, le=1000)

class ReplyInput(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)

class MeetingInput(BaseModel):
    contact_id: int
    starts_at: float
    duration_minutes: int = Field(default=30, ge=10, le=240)
    location: str = Field(default='', max_length=1000)
    notes: str = Field(default='', max_length=4000)

class KnowledgeInput(BaseModel):
    text: str = Field(min_length=10,max_length=2000)
    source_message_id: int | None = None

class BookingInput(BaseModel):
    workspace_id: int
    contact_id: int
    token: str = Field(min_length=20,max_length=150)
    starts_at: float = Field(gt=0,allow_inf_nan=False)

def create_app(settings=None):
    settings = settings or Settings.from_env()
    engine, factory = make_store(settings.database_url)
    vault = Vault(settings.secret_key)
    app = FastAPI(title='Egasis', docs_url=None if settings.production else '/docs', redoc_url=None)
    app.state.factory, app.state.settings, app.state.vault = factory, settings, vault
    attempts, attempts_lock = defaultdict(list), Lock()

    @app.middleware('http')
    async def boundary(request, call_next):
        if request.method in {'POST','PUT','PATCH','DELETE'} and request.url.path not in {'/api/billing/webhook','/api/unsubscribe'}:
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != settings.public_url:
                return PlainTextResponse('Origen no permitido', status_code=403)
        result = await call_next(request)
        result.headers['X-Content-Type-Options'] = 'nosniff'
        result.headers['Referrer-Policy'] = 'same-origin'
        result.headers['X-Frame-Options'] = 'DENY'
        result.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith('/api/'):
            result.headers['Cache-Control'] = 'no-store'
        return result

    def db_session():
        with factory() as db:
            yield db

    def current(request: Request, db=Depends(db_session)):
        token = request.cookies.get('egasis_session', '')
        session = db.get(Session, token_hash(token)) if token else None
        if not session or session.expires_at < time.time():
            raise HTTPException(401, 'Iniciá sesión para continuar.')
        user = db.get(User, session.user_id)
        if not user:
            raise HTTPException(401, 'Sesión inválida.')
        return user

    def owner(user=Depends(current)):
        if user.role != 'owner':
            raise HTTPException(403, 'Esta acción requiere acceso de propietario.')
        return user

    def scoped(db, model, identity, wid):
        row = db.scalar(select(model).where(model.id == identity, model.workspace_id == wid))
        if not row:
            raise HTTPException(404, 'No se encontró ese registro.')
        return row

    def lock_workspace(db, wid):
        # Authentication may have opened a read transaction. Begin the write
        # lock before checking mutable records to serialize owner actions.
        db.rollback()
        if db.bind.dialect.name=='sqlite': db.execute(text('BEGIN IMMEDIATE'))
        else: db.execute(select(Workspace.id).where(Workspace.id==wid).with_for_update())

    def issue_session(db, response, user):
        token = secrets.token_urlsafe(40)
        db.add(Session(token_hash=token_hash(token), user_id=user.id, expires_at=time.time()+86400*7))
        db.commit()
        response.set_cookie('egasis_session', token, max_age=86400*7, httponly=True, secure=settings.production, samesite='strict')

    def throttle(request):
        address = request.client.host if request.client else 'unknown'
        now = time.time()
        with attempts_lock:
            attempts[address] = [t for t in attempts[address] if t > now-300]
            if len(attempts[address]) >= 12:
                raise HTTPException(429, 'Demasiados intentos. Probá dentro de cinco minutos.')
            attempts[address].append(now)

    @app.get('/api/health')
    def health():
        return {'status':'ok', 'name':'Egasis', 'simulation':settings.simulation}

    @app.post('/api/register')
    def register(data: Register, request: Request, response: Response, db=Depends(db_session)):
        throttle(request)
        if settings.production and (not settings.registration_token or not hmac.compare_digest(data.invite, settings.registration_token)):
            raise HTTPException(403, 'Necesitás una invitación para crear tu espacio.')
        email = valid_email(data.email)
        workspace = Workspace(name=data.name)
        db.add(workspace); db.flush()
        user = User(workspace_id=workspace.id, email=email, password_hash=password_hash(data.password))
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, 'No se pudo crear la cuenta con ese correo.')
        issue_session(db, response, user)
        return {'id':user.id, 'workspace_id':workspace.id}

    @app.post('/api/login')
    def login(data: Login, request: Request, response: Response, db=Depends(db_session)):
        throttle(request)
        user = db.scalar(select(User).where(User.email == data.email.strip().lower()))
        if not user or not verify_password(data.password, user.password_hash):
            raise HTTPException(401, 'Correo o contraseña incorrectos.')
        issue_session(db, response, user)
        return {'id':user.id}

    @app.post('/api/logout')
    def logout(request: Request, response: Response, db=Depends(db_session)):
        session = db.get(Session, token_hash(request.cookies.get('egasis_session','')))
        if session:
            db.delete(session); db.commit()
        response.delete_cookie('egasis_session')
        return {'ok':True}

    @app.get('/api/me')
    def me(user=Depends(current), db=Depends(db_session)):
        return {'user':record(user, ('password_hash',)), 'workspace':record(db.get(Workspace,user.workspace_id)), 'simulation':settings.simulation}

    @app.put('/api/workspace')
    def workspace_update(data: WorkspaceInput, user=Depends(owner), db=Depends(db_session)):
        try:
            ZoneInfo(data.timezone)
        except ZoneInfoNotFoundError:
            raise HTTPException(422, 'Zona horaria inválida.')
        row = db.get(Workspace,user.workspace_id)
        for k,v in data.model_dump().items(): setattr(row,k,v)
        db.commit(); return record(row)

    @app.get('/api/campaigns')
    def campaigns(user=Depends(current), db=Depends(db_session)):
        rows = db.scalars(select(Campaign).where(Campaign.workspace_id==user.workspace_id).order_by(Campaign.id.desc())).all()
        result=[]
        for row in rows:
            d=record(row)
            d['contacts']=db.scalar(select(func.count(Contact.id)).where(Contact.campaign_id==row.id,Contact.workspace_id==user.workspace_id))
            result.append(d)
        return result

    def validate_campaign(data):
        if not re.fullmatch(r'[0-6](,[0-6])*',data.weekdays): raise HTTPException(422,'Elegí días válidos.')
        if data.start_hour >= data.end_hour: raise HTTPException(422,'El horario final debe ser posterior al inicial.')
        if data.reply_mode not in {'review','automatic'}: raise HTTPException(422,'Modo de respuesta inválido.')
        fields=set(re.findall(r'\{\{(.*?)\}\}',data.subject+data.body+data.auto_reply_body))
        if fields-{'name','company','offer','signature','booking_link'}: raise HTTPException(422,'Variables permitidas: name, company, offer, signature, booking_link.')
        if '\n' in data.subject or '\r' in data.subject: raise HTTPException(422,'El asunto debe ocupar una línea.')
        if data.reply_mode=='automatic' and len(data.auto_reply_body.strip())<10: raise HTTPException(422,'Configurá una plantilla para las respuestas automáticas.')

    @app.post('/api/campaigns')
    def create_campaign(data: CampaignInput, user=Depends(owner), db=Depends(db_session)):
        validate_campaign(data)
        row=Campaign(workspace_id=user.workspace_id,**data.model_dump()); db.add(row); db.commit(); return record(row)

    @app.put('/api/campaigns/{cid}')
    def update_campaign(cid:int,data:CampaignInput,user=Depends(owner),db=Depends(db_session)):
        validate_campaign(data); row=scoped(db,Campaign,cid,user.workspace_id)
        if row.status=='active': raise HTTPException(409,'Pausá la campaña antes de editarla.')
        queued=db.scalar(select(func.count(Job.id)).join(Message,Job.message_id==Message.id).join(Contact,Message.contact_id==Contact.id).where(Contact.campaign_id==cid,Job.status.in_(['pending','running'])))
        if queued: raise HTTPException(409,'La campaña tiene mensajes preparados. Cancelá la cola antes de cambiar el contenido.')
        for k,v in data.model_dump().items(): setattr(row,k,v)
        db.commit(); return record(row)

    @app.post('/api/campaigns/{cid}/{action}')
    def campaign_action(cid:int,action:str,user=Depends(owner),db=Depends(db_session)):
        row=scoped(db,Campaign,cid,user.workspace_id)
        if action=='start':
            workspace=db.get(Workspace,user.workspace_id)
            if not (row.offer or workspace.offer).strip(): raise HTTPException(422,'Configurá la oferta antes de activar la campaña.')
            if not db.scalar(select(Account.id).where(Account.workspace_id==user.workspace_id,Account.active==True)): raise HTTPException(422,'Conectá una cuenta de correo antes de activar la campaña.')
            if not settings.simulation and urlparse(settings.public_url).hostname in {'localhost','127.0.0.1'}: raise HTTPException(422,'Configurá una dirección pública HTTPS para gestionar bajas.')
            if workspace.subscription_status not in {'pilot','active','trialing'}: raise HTTPException(403,'La suscripción no permite activar envíos.')
            if row.reply_mode=='automatic' and not row.auto_reply_body.strip(): raise HTTPException(422,'Configurá la plantilla para respuestas automáticas.')
            from .mail import MailEngine
            try:
                count=MailEngine(factory,vault,settings.simulation,settings.public_url).prepare_campaign(user.workspace_id,cid)
            except ValueError as exc:
                raise HTTPException(422,str(exc))
            row.status='active'; db.commit()
            db.add(Event(workspace_id=user.workspace_id,kind='campaign_started',detail=f'{row.name}: {count} mensajes preparados')); db.commit()
            return {'status':'active','queued':count}
        if action in {'pause','stop'}:
            row.status='paused'; db.commit()
            if action=='stop':
                messages=db.scalars(select(Message).join(Contact,Message.contact_id==Contact.id).where(Contact.campaign_id==cid,Message.workspace_id==user.workspace_id,Message.status=='queued')).all()
                for msg in messages:
                    msg.status='cancelled'
                    job=db.scalar(select(Job).where(Job.message_id==msg.id,Job.status=='pending'))
                    if job: job.status='cancelled'
                db.commit()
            return {'status':row.status}
        raise HTTPException(404,'Acción no encontrada.')

    @app.get('/api/contacts')
    def contacts(campaign_id:int|None=None,q:str='',status:str='',user=Depends(current),db=Depends(db_session)):
        stmt=select(Contact).where(Contact.workspace_id==user.workspace_id)
        if campaign_id: stmt=stmt.where(Contact.campaign_id==campaign_id)
        if q: stmt=stmt.where((Contact.company.ilike(f'%{q[:100]}%')) | (Contact.email.ilike(f'%{q[:100]}%')) | (Contact.name.ilike(f'%{q[:100]}%')))
        if status: stmt=stmt.where(Contact.status==status)
        return [record(r) for r in db.scalars(stmt.order_by(Contact.id.desc()).limit(1000)).all()]

    @app.post('/api/contacts/import/{cid}')
    def import_contacts(cid:int,data:ContactsInput,user=Depends(owner),db=Depends(db_session)):
        campaign=scoped(db,Campaign,cid,user.workspace_id)
        if campaign.status=='active': raise HTTPException(409,'Pausá la campaña antes de importar.')
        count=0; duplicates=0
        for item in data.contacts:
            email=valid_email(item.email)
            if db.scalar(select(Contact.id).where(Contact.workspace_id==user.workspace_id,Contact.campaign_id==cid,Contact.email==email)):
                duplicates+=1; continue
            suppressed=db.scalar(select(Suppression.id).where(Suppression.workspace_id==user.workspace_id,Suppression.email==email))
            db.add(Contact(workspace_id=user.workspace_id,campaign_id=cid,**{**item.model_dump(),'email':email},status='do_not_contact' if suppressed else 'pending',source='import'))
            db.flush();count+=1
        db.commit();return {'imported':count,'duplicates':duplicates}

    @app.get('/api/export/contacts')
    def export_contacts(user=Depends(current),db=Depends(db_session)):
        stream=io.StringIO();writer=csv.writer(stream);writer.writerow(['email','name','company','status','source'])
        for row in db.scalars(select(Contact).where(Contact.workspace_id==user.workspace_id)).all():
            writer.writerow([("'"+v if v.startswith(('=','+','-','@','\t','\r')) else v) for v in [row.email,row.name,row.company,row.status,row.source]])
        return Response(stream.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="egasis-contactos.csv"'})

    @app.post('/api/contacts/{cid}/suppress')
    def suppress_contact(cid:int,user=Depends(owner),db=Depends(db_session)):
        contact=scoped(db,Contact,cid,user.workspace_id)
        suppress(db,user.workspace_id,contact.email,'manual')
        return {'ok':True}

    def suppress(db,wid,email,reason):
        workspace=db.get(Workspace,wid,with_for_update=True)
        if not workspace: raise HTTPException(404,'Espacio no encontrado.')
        if not db.scalar(select(Suppression.id).where(Suppression.workspace_id==wid,Suppression.email==email)):
            db.add(Suppression(workspace_id=wid,email=email,reason=reason))
        for c in db.scalars(select(Contact).where(Contact.workspace_id==wid,Contact.email==email)).all():
            c.status='do_not_contact'
            for msg in db.scalars(select(Message).where(Message.contact_id==c.id,Message.status.in_(['draft','queued']))).all():
                msg.status='cancelled'
                job=db.scalar(select(Job).where(Job.message_id==msg.id,Job.status=='pending'))
                if job: job.status='cancelled'
        db.commit()

    @app.api_route('/api/unsubscribe',methods=['GET','POST'])
    def unsubscribe(workspace_id:int,email:str,token:str,request:Request,db=Depends(db_session)):
        email=valid_email(email)
        if not vault.verify_unsubscribe(workspace_id,email,token): raise HTTPException(403,'Enlace inválido.')
        if request.method=='GET':
            action=html.escape('/api/unsubscribe?'+urlencode({'workspace_id':workspace_id,'email':email,'token':token}),quote=True)
            return HTMLResponse(f'<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Cancelar suscripción · Egasis</title><link rel="stylesheet" href="/static/style.css"><main style="max-width:560px;margin:12vh auto;padding:30px"><h1>Dejá de recibir estos correos.</h1><p>Confirmá la baja de {html.escape(email)}. Se aplicará a futuros mensajes de este remitente.</p><form action="{action}" method="post"><button class="primary">Confirmar baja</button></form></main></html>')
        suppress(db,workspace_id,email,'unsubscribe')
        return PlainTextResponse('Listo. No recibirás más mensajes de este espacio de Egasis.')

    @app.get('/api/accounts')
    def accounts(user=Depends(current),db=Depends(db_session)):
        return [record(a,('secret',)) for a in db.scalars(select(Account).where(Account.workspace_id==user.workspace_id)).all()]

    @app.post('/api/accounts')
    def account_create(data:AccountInput,user=Depends(owner),db=Depends(db_session)):
        values=data.model_dump();secret=values.pop('password');values['email']=valid_email(values['email'])
        # Email connections are restricted to supported public provider endpoints.
        if values['smtp_host'] not in {'smtp.gmail.com','smtp.office365.com','smtp.mail.yahoo.com'} or values['imap_host'] not in {'imap.gmail.com','outlook.office365.com','imap.mail.yahoo.com'}:
            raise HTTPException(422,'Proveedor no habilitado. Usá Gmail, Microsoft 365 o Yahoo.')
        if values['smtp_port'] not in {465,587} or values['imap_port']!=993: raise HTTPException(422,'Usá los puertos seguros del proveedor.')
        row=Account(workspace_id=user.workspace_id,secret=vault.encrypt(secret),**values);db.add(row)
        try: db.commit()
        except IntegrityError: db.rollback();raise HTTPException(409,'Esa cuenta ya está conectada.')
        return record(row,('secret',))

    @app.post('/api/accounts/{aid}/toggle')
    def account_toggle(aid:int,user=Depends(owner),db=Depends(db_session)):
        row=scoped(db,Account,aid,user.workspace_id);row.active=not row.active;db.commit();return record(row,('secret',))

    @app.get('/api/conversations')
    def conversations(user=Depends(current),db=Depends(db_session)):
        rows=db.scalars(select(Contact).where(Contact.workspace_id==user.workspace_id,Contact.id.in_(select(Message.contact_id).where(Message.direction=='inbound',Message.workspace_id==user.workspace_id))).order_by(Contact.id.desc())).all()
        result=[]
        for contact in rows:
            messages=db.scalars(select(Message).where(Message.contact_id==contact.id,Message.workspace_id==user.workspace_id).order_by(Message.created_at,Message.id)).all()
            result.append({'contact':record(contact),'messages':[record(m) for m in messages]})
        return result

    @app.post('/api/messages/{mid}/reply')
    def reply(mid:int,data:ReplyInput,user=Depends(owner),db=Depends(db_session)):
        lock_workspace(db,user.workspace_id)
        inbound=scoped(db,Message,mid,user.workspace_id)
        if inbound.direction!='inbound': raise HTTPException(422,'Elegí el mensaje recibido que querés responder.')
        if '\n' in data.subject or '\r' in data.subject: raise HTTPException(422,'El asunto debe ocupar una línea.')
        latest=db.scalar(select(Message.id).where(Message.workspace_id==user.workspace_id,Message.contact_id==inbound.contact_id,Message.direction=='inbound').order_by(Message.created_at.desc(),Message.id.desc()).limit(1))
        if latest!=mid: raise HTTPException(409,'Hay una respuesta más reciente. Actualizá la conversación.')
        contact=scoped(db,Contact,inbound.contact_id,user.workspace_id)
        if db.scalar(select(Suppression.id).where(Suppression.workspace_id==user.workspace_id,Suppression.email==contact.email)):
            raise HTTPException(409,'Este contacto pidió no recibir más mensajes.')
        key=f'reply:{mid}'
        existing=db.scalar(select(Message).where(Message.workspace_id==user.workspace_id,Message.idempotency_key==key))
        if existing and existing.status not in {'draft','failed'}: return record(existing)
        if existing:
            existing.subject=data.subject;existing.body=data.body;existing.status='queued';existing.error=''
            job=db.scalar(select(Job).where(Job.message_id==existing.id))
            if job: job.status='pending';job.due_at=time.time();job.error=''
            else: db.add(Job(workspace_id=user.workspace_id,message_id=existing.id))
        else:
            existing=Message(workspace_id=user.workspace_id,contact_id=contact.id,account_id=inbound.account_id,direction='outbound',subject=data.subject,body=data.body,status='queued',idempotency_key=key,in_reply_to=inbound.provider_id or '')
            db.add(existing);db.flush();db.add(Job(workspace_id=user.workspace_id,message_id=existing.id))
        db.commit();return record(existing)

    @app.post('/api/messages/{mid}/dismiss')
    def dismiss(mid:int,user=Depends(owner),db=Depends(db_session)):
        msg=scoped(db,Message,mid,user.workspace_id)
        if msg.status not in {'draft','received'}: raise HTTPException(409,'No se puede descartar este mensaje.')
        msg.status='dismissed';db.commit();return {'ok':True}

    @app.post('/api/ai/research/{cid}')
    def research(cid:int,user=Depends(owner),db=Depends(db_session)):
        scoped(db,Contact,cid,user.workspace_id)
        from .intelligence import Intelligence
        try: return Intelligence(factory,vault).research(user.workspace_id,cid)
        except (ValueError,RuntimeError) as exc: raise HTTPException(422,str(exc))

    @app.post('/api/ai/draft/{mid}')
    def draft(mid:int,user=Depends(owner),db=Depends(db_session)):
        msg=scoped(db,Message,mid,user.workspace_id)
        from .intelligence import Intelligence
        try:
            result=Intelligence(factory,vault).draft_reply(user.workspace_id,msg.contact_id,mid)
            return record(result) if isinstance(result,Message) else result
        except (ValueError,RuntimeError) as exc: raise HTTPException(422,str(exc))

    @app.get('/api/connections')
    def connections(user=Depends(owner),db=Depends(db_session)):
        return [{'provider':c.provider,'configured':True} for c in db.scalars(select(Connection).where(Connection.workspace_id==user.workspace_id)).all()]

    @app.put('/api/connections/{provider}')
    def connection(provider:str,data:dict,user=Depends(owner),db=Depends(db_session)):
        if provider not in {'gemini','apollo','google_calendar'}: raise HTTPException(422,'Conector no habilitado.')
        secret=data.get('secret','')
        if not isinstance(secret,str) or not 10<=len(secret)<=16000: raise HTTPException(422,'Clave inválida.')
        if provider=='google_calendar':
            try:
                parsed=json.loads(secret)
                if not isinstance(parsed,dict) or not parsed.get('calendar_id') or not (parsed.get('access_token') or parsed.get('refresh_token')):raise ValueError()
            except (ValueError,TypeError):raise HTTPException(422,'Configuración de calendario inválida.')
        row=db.scalar(select(Connection).where(Connection.workspace_id==user.workspace_id,Connection.provider==provider))
        if row: row.secret=vault.encrypt(secret)
        else: db.add(Connection(workspace_id=user.workspace_id,provider=provider,secret=vault.encrypt(secret)))
        db.commit();return {'configured':True}

    @app.get('/api/knowledge')
    def knowledge_list(user=Depends(current)):
        from .knowledge import KnowledgeService,KnowledgeError
        return KnowledgeService(factory).list(user.workspace_id)

    @app.post('/api/knowledge')
    def knowledge_create(data:KnowledgeInput,user=Depends(owner)):
        from .knowledge import KnowledgeService,KnowledgeError
        try:return KnowledgeService(factory).create(user.workspace_id,data.text,data.source_message_id)
        except KnowledgeError as exc:raise HTTPException(exc.status_code,str(exc))

    @app.post('/api/knowledge/{nid}/{action}')
    def knowledge_action(nid:int,action:str,user=Depends(owner)):
        from .knowledge import KnowledgeService,KnowledgeError
        if action not in {'approve','archive'}:raise HTTPException(404,'Acción no encontrada.')
        try:return KnowledgeService(factory).change_state(user.workspace_id,nid,'approved' if action=='approve' else 'archived')
        except KnowledgeError as exc:raise HTTPException(exc.status_code,str(exc))

    @app.get('/api/meetings')
    def meetings(user=Depends(current),db=Depends(db_session)):
        return [{**record(m),'contact':record(db.get(Contact,m.contact_id))} for m in db.scalars(select(Meeting).where(Meeting.workspace_id==user.workspace_id).order_by(Meeting.starts_at)).all()]

    @app.post('/api/meetings')
    def meeting_create(data:MeetingInput,user=Depends(owner),db=Depends(db_session)):
        scoped(db,Contact,data.contact_id,user.workspace_id)
        if data.starts_at < time.time(): raise HTTPException(422,'Elegí un horario futuro.')
        row=Meeting(workspace_id=user.workspace_id,**data.model_dump());db.add(row);db.commit();return record(row)

    @app.post('/api/meetings/{mid}/{action}')
    def meeting_action(mid:int,action:str,user=Depends(owner),db=Depends(db_session)):
        row=scoped(db,Meeting,mid,user.workspace_id)
        if action not in {'confirm','cancel'}: raise HTTPException(404,'Acción no encontrada.')
        connected=db.scalar(select(Connection.id).where(Connection.workspace_id==user.workspace_id,Connection.provider=='google_calendar'))
        if connected:
            from .calendar_service import CalendarService,CalendarError
            try:
                service=CalendarService(factory,vault)
                return service.create_event(user.workspace_id,mid) if action=='confirm' else service.cancel_event(user.workspace_id,mid)
            except CalendarError as exc:raise HTTPException(exc.status_code,str(exc))
        lock_workspace(db,user.workspace_id)
        row=scoped(db,Meeting,mid,user.workspace_id)
        if action=='confirm':
            if row.status=='cancelled': raise HTTPException(409,'La reunión fue cancelada. Proponé un nuevo horario.')
            ends=row.starts_at+row.duration_minutes*60
            other=db.scalar(select(Meeting.id).where(Meeting.workspace_id==user.workspace_id,Meeting.id!=mid,Meeting.status.in_(['confirmed','reserving']),Meeting.starts_at<ends,(Meeting.starts_at+Meeting.duration_minutes*60)>row.starts_at))
            if other: raise HTTPException(409,'Ya tenés una reunión confirmada en ese horario.')
        row.status='confirmed' if action=='confirm' else 'cancelled'
        db.add(Event(workspace_id=user.workspace_id,kind='meeting_'+row.status,detail=f'Reunión {mid}: confirmación manual del propietario' if action=='confirm' else f'Reunión {mid} cancelada'))
        db.commit();return record(row)

    @app.get('/api/calendar/{mid}.ics')
    def calendar(mid:int,user=Depends(current),db=Depends(db_session)):
        row=scoped(db,Meeting,mid,user.workspace_id);contact=scoped(db,Contact,row.contact_id,user.workspace_id)
        def escape(v):return v.replace('\\','\\\\').replace('\r','').replace('\n','\\n').replace(';','\\;').replace(',','\\,')
        def stamp(v):return datetime.fromtimestamp(v,timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//Egasis//Meetings//ES','BEGIN:VEVENT',f'UID:meeting-{row.id}@egasis',f'DTSTAMP:{stamp(time.time())}',f'DTSTART:{stamp(row.starts_at)}',f'DTEND:{stamp(row.starts_at+row.duration_minutes*60)}',f'SUMMARY:{escape("Reunión con "+(contact.company or contact.name or contact.email))}',f'LOCATION:{escape(row.location)}',f'DESCRIPTION:{escape(row.notes)}','STATUS:'+('CONFIRMED' if row.status=='confirmed' else 'CANCELLED' if row.status=='cancelled' else 'TENTATIVE'),'END:VEVENT','END:VCALENDAR']
        return Response('\r\n'.join(lines)+'\r\n',media_type='text/calendar',headers={'Content-Disposition':f'attachment; filename="reunion-{mid}.ics"'})

    @app.get('/api/materials/{cid}/{format}')
    def materials(cid:int,format:str,user=Depends(current),db=Depends(db_session)):
        scoped(db,Contact,cid,user.workspace_id)
        from .materials import build_brief,export_markdown,export_pptx
        brief=build_brief(factory,user.workspace_id,cid)
        if format=='json':return brief
        if format=='md':
            return StreamingResponse(export_markdown(brief),media_type='text/markdown; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="egasis-ficha-{cid}.md"'})
        if format=='pptx':
            return StreamingResponse(export_pptx(brief),media_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',headers={'Content-Disposition':f'attachment; filename="egasis-presentacion-{cid}.pptx"'})
        raise HTTPException(404,'Formato no disponible.')

    @app.get('/api/contacts/{cid}/booking-link')
    def get_booking_link(cid:int,user=Depends(current),db=Depends(db_session)):
        scoped(db,Contact,cid,user.workspace_id)
        from .booking import booking_link
        return {'url':booking_link(settings,vault,user.workspace_id,cid)}

    @app.get('/api/booking')
    def booking_availability(workspace_id:int,contact_id:int,token:str):
        from .booking import BookingService,BookingError
        from .calendar_service import CalendarError
        try:return BookingService(factory,vault).availability(workspace_id,contact_id,token)
        except BookingError as exc:raise HTTPException(422,str(exc))
        except CalendarError as exc:raise HTTPException(503,'No se pudo comprobar la disponibilidad del calendario.')

    @app.post('/api/booking')
    def book(data:BookingInput):
        from .booking import BookingService,BookingError
        from .calendar_service import CalendarError
        try:return BookingService(factory,vault).reserve(data.workspace_id,data.contact_id,data.token,data.starts_at)
        except BookingError as exc:raise HTTPException(409,str(exc))
        except CalendarError as exc:raise HTTPException(503,'No se pudo comprobar la disponibilidad del calendario.')

    @app.get('/api/sources/apollo')
    def apollo_status(user=Depends(owner)):
        from .prospect_sources import ApolloSource
        return ApolloSource(factory,vault).readiness(user.workspace_id)

    @app.post('/api/sources/apollo/search/{cid}')
    def apollo_search(cid:int,data:dict,user=Depends(owner),db=Depends(db_session)):
        scoped(db,Campaign,cid,user.workspace_id)
        from .prospect_sources import ApolloSource,SourceError
        allowed={'keywords','titles','person_locations','organization_locations','seniorities','employee_ranges','domains','page','per_page','include_similar_titles'}
        try:return ApolloSource(factory,vault).search(user.workspace_id,cid,**{k:v for k,v in data.items() if k in allowed})
        except SourceError as exc:raise HTTPException(exc.http_status,str(exc))

    @app.post('/api/sources/apollo/import/{cid}')
    def apollo_import(cid:int,data:dict,user=Depends(owner),db=Depends(db_session)):
        campaign=scoped(db,Campaign,cid,user.workspace_id)
        if campaign.status=='active':raise HTTPException(409,'Pausá la campaña antes de importar.')
        from .prospect_sources import ApolloSource,SourceError
        try:result=ApolloSource(factory,vault).enrich(user.workspace_id,cid,data.get('person_id',''),allow_credit_use=data.get('allow_credit_use') is True)
        except SourceError as exc:raise HTTPException(exc.http_status,str(exc))
        contact=result['contact']
        if not contact.get('importable'):return {'imported':False,'result':result}
        email=valid_email(contact['email'])
        if db.scalar(select(Suppression.id).where(Suppression.workspace_id==user.workspace_id,Suppression.email==email)):raise HTTPException(409,'Este contacto está excluido.')
        existing=db.scalar(select(Contact).where(Contact.workspace_id==user.workspace_id,Contact.campaign_id==cid,Contact.email==email))
        if existing:return {'imported':False,'duplicate':True,'contact':record(existing)}
        row=Contact(workspace_id=user.workspace_id,campaign_id=cid,email=email,name=contact.get('name') or '',company=contact.get('company') or '',website=contact.get('website') or '',source='apollo',evidence=json.dumps({'source_metadata':contact.get('source_metadata',{})},ensure_ascii=False))
        db.add(row);db.commit();return {'imported':True,'contact':record(row)}

    @app.get('/api/metrics')
    def metrics(user=Depends(current),db=Depends(db_session)):
        wid=user.workspace_id;day=time.time()-86400
        def count(model,*conditions):return db.scalar(select(func.count()).select_from(model).where(model.workspace_id==wid,*conditions)) or 0
        result={'contacts':count(Contact),'sent':count(Message,Message.direction=='outbound',Message.status=='sent'),'simulated':count(Message,Message.status=='simulated'),'replies':count(Message,Message.direction=='inbound',Message.classification.not_in(['bounce','out_of_office'])),'bounces':count(Message,Message.direction=='inbound',Message.classification=='bounce'),'received':count(Message,Message.direction=='inbound'),'interested':count(Contact,Contact.status=='interested'),'meetings':count(Meeting,Meeting.status=='confirmed'),'queued':count(Job,Job.status=='pending'),'issues':count(Message,Message.status.in_(['failed','uncertain'])),'active_campaigns':count(Campaign,Campaign.status=='active'),'cost_24h':round(db.scalar(select(func.sum(Usage.cost)).where(Usage.workspace_id==wid,Usage.created_at>=day)) or 0,4),'simulation':settings.simulation}
        result['campaign_results']=[]
        for campaign in db.scalars(select(Campaign).where(Campaign.workspace_id==wid).order_by(Campaign.id.desc()).limit(100)):
            members=select(Contact.id).where(Contact.workspace_id==wid,Contact.campaign_id==campaign.id)
            result['campaign_results'].append({'id':campaign.id,'name':campaign.name,
                'sent':count(Message,Message.contact_id.in_(members),Message.direction=='outbound',Message.status=='sent'),
                'simulated':count(Message,Message.contact_id.in_(members),Message.status=='simulated'),
                'replies':count(Message,Message.contact_id.in_(members),Message.direction=='inbound',Message.classification.not_in(['bounce','out_of_office'])),
                'meetings':count(Meeting,Meeting.contact_id.in_(members),Meeting.status=='confirmed'),
                'cost_24h':round(db.scalar(select(func.sum(Usage.cost)).where(Usage.workspace_id==wid,Usage.contact_id.in_(members),Usage.created_at>=day)) or 0,4)})
        result['events']=[record(e) for e in db.scalars(select(Event).where(Event.workspace_id==wid).order_by(Event.id.desc()).limit(12)).all()]
        result['uncertain_messages']=[record(m) for m in db.scalars(select(Message).where(Message.workspace_id==wid,Message.status=='uncertain').order_by(Message.id).limit(100)).all()]
        result['recent_messages']=[record(m) for m in db.scalars(select(Message).where(Message.workspace_id==wid).order_by(Message.id.desc()).limit(12)).all()]
        return result

    @app.post('/api/messages/{mid}/reconcile')
    def reconcile(mid:int,data:dict,user=Depends(owner),db=Depends(db_session)):
        scoped(db,Message,mid,user.workspace_id)
        from .mail import MailEngine
        try:
            return MailEngine(factory,vault,settings.simulation,settings.public_url).reconcile(user.workspace_id,mid,data.get('outcome'),data.get('evidence',''),data.get('accepted_at'))
        except ValueError as exc: raise HTTPException(422,str(exc))

    @app.post('/api/demo')
    def demo(user=Depends(owner),db=Depends(db_session)):
        if not settings.simulation: raise HTTPException(403,'Los ejemplos solo están disponibles en simulación.')
        if db.scalar(select(Campaign.id).where(Campaign.workspace_id==user.workspace_id)): raise HTTPException(409,'Tu espacio ya tiene campañas. Los ejemplos se cargan solo en un espacio vacío.')
        workspace=db.get(Workspace,user.workspace_id);workspace.offer='Diseño y renovación de espacios comerciales';workspace.audience='Comercios que buscan renovar su local';workspace.signature='El equipo comercial'
        campaign=Campaign(workspace_id=user.workspace_id,name='Renovación de locales · ejemplo',offer=workspace.offer,audience=workspace.audience,subject='Una idea para {{company}}',body='Hola {{name}},\n\nTrabajamos en {{offer}}. ¿Están evaluando cambios en {{company}} este año? Si tiene sentido, podemos conversar 15 minutos.\n\n{{signature}}')
        db.add(campaign);db.flush()
        account=Account(workspace_id=user.workspace_id,email='equipo@example.com',display_name='Cuenta de ejemplo',secret=vault.encrypt('simulation-only'));db.add(account);db.flush()
        for name,company,addr,status in [('Lucía','Casa Norte','lucia','interested'),('Mateo','Estudio Sur','mateo','pending'),('Sofía','Mercado Central','sofia','pending')]:
            c=Contact(workspace_id=user.workspace_id,campaign_id=campaign.id,account_id=account.id,email=addr+'@example.com',name=name,company=company,source='demo',status=status)
            db.add(c);db.flush()
            if status=='interested': db.add(Message(workspace_id=user.workspace_id,contact_id=c.id,account_id=account.id,direction='inbound',subject='Renovación del local',body='Hola, estamos evaluando renovar el espacio el mes que viene. ¿Podrían contarme cómo trabajan?',status='received',classification='interested',provider_id='<demo-inbound@example.com>'))
        db.add(Event(workspace_id=user.workspace_id,kind='demo_loaded',detail='Ejemplos cargados. Todos los correos son de simulación.'));db.commit();return {'ok':True}

    @app.get('/api/billing')
    def billing_status(user=Depends(owner),db=Depends(db_session)):
        from .billing import BillingService
        return {**BillingService(factory).readiness(),'workspace':record(db.get(Workspace,user.workspace_id))}

    @app.post('/api/billing/checkout/{tier}')
    def checkout(tier:str,user=Depends(owner)):
        from .billing import BillingService,BillingError
        try: return BillingService(factory).checkout(user.workspace_id,user.id,tier)
        except BillingError as exc: raise HTTPException(getattr(exc,'status_code',422),str(exc))

    @app.post('/api/billing/portal')
    def portal(user=Depends(owner)):
        from .billing import BillingService,BillingError
        try: return BillingService(factory).portal(user.workspace_id,user.id)
        except BillingError as exc: raise HTTPException(getattr(exc,'status_code',422),str(exc))

    @app.post('/api/billing/webhook')
    async def billing_webhook(request:Request):
        from .billing import BillingService,BillingError
        payload=await request.body()
        if len(payload)>1000000: raise HTTPException(413,'Evento demasiado grande.')
        try: return BillingService(factory).webhook(payload,request.headers.get('stripe-signature',''))
        except BillingError as exc: raise HTTPException(getattr(exc,'status_code',400),str(exc))

    @app.post('/api/engine/tick')
    def simulation_tick(user=Depends(owner)):
        if not settings.simulation: raise HTTPException(403,'La ejecución manual está disponible en simulación. El worker procesa los envíos reales.')
        from .mail import MailEngine
        from .worker import tick
        return tick(factory,vault,settings,user.workspace_id)

    @app.get('/')
    def index(): return FileResponse(STATIC/'index.html')
    @app.get('/book')
    def booking_page(): return FileResponse(STATIC/'booking.html')
    app.mount('/static',StaticFiles(directory=STATIC),name='static')
    return app

app = create_app()
