"""Recipient-confirmed reservations in Egasis, with optional Google Calendar."""
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from sqlalchemy import select, text
from .models import Campaign, Connection, Contact, Event, Meeting, Workspace

class BookingError(ValueError):
    pass

def booking_link(settings,vault,wid,cid):
    return settings.public_url+'/book?'+urlencode({'workspace_id':wid,'contact_id':cid,'token':vault.booking_token(wid,cid)})

class BookingService:
    def __init__(self,factory,vault):
        self.factory,self.vault=factory,vault

    def _records(self,db,wid,cid,token):
        if not self.vault.verify_booking(wid,cid,token):raise BookingError('El enlace venció o no es válido.')
        contact=db.scalar(select(Contact).where(Contact.id==cid,Contact.workspace_id==wid))
        workspace=db.get(Workspace,wid)
        if not contact or not workspace:raise BookingError('No se encontró esta invitación.')
        campaign=db.get(Campaign,contact.campaign_id)
        if not campaign or campaign.workspace_id!=wid:raise BookingError('Invitación inválida.')
        return workspace,contact,campaign

    def _slots(self,workspace,campaign,busy):
        now=time.time();zone=ZoneInfo(workspace.timezone);day=datetime.fromtimestamp(now,zone).replace(hour=0,minute=0,second=0,microsecond=0)
        allowed={int(d) for d in campaign.weekdays.split(',')};slots=[]
        for offset in range(10):
            date=day+timedelta(days=offset)
            if date.weekday() not in allowed:continue
            for half in range(campaign.start_hour*2,campaign.end_hour*2):
                start=(date+timedelta(minutes=half*30)).timestamp();end=start+1800
                if start<now+3600 or any(start<b['end'] and end>b['start'] for b in busy):continue
                slots.append(start)
                if len(slots)>=40:return slots
        return slots

    def availability(self,wid,cid,token):
        with self.factory() as db:
            workspace,contact,campaign=self._records(db,wid,cid,token)
            existing=db.scalar(select(Meeting).where(Meeting.workspace_id==wid,Meeting.contact_id==cid,Meeting.status.in_(['confirmed','reserving']),Meeting.starts_at>time.time()).order_by(Meeting.starts_at).limit(1))
            if existing:
                return {'business':workspace.name,'name':contact.name,'timezone':workspace.timezone,'duration_minutes':existing.duration_minutes,'slots':[],'reservation':{'starts_at':existing.starts_at,'status':existing.status}}
            busy=[{'start':m.starts_at,'end':m.starts_at+m.duration_minutes*60} for m in db.scalars(select(Meeting).where(Meeting.workspace_id==wid,Meeting.status.in_(['confirmed','reserving']),Meeting.starts_at>time.time()-86400)).all()]
            external=bool(db.scalar(select(Connection.id).where(Connection.workspace_id==wid,Connection.provider=='google_calendar')))
        if external:
            from .calendar_service import CalendarService
            result=CalendarService(self.factory,self.vault).availability(wid,time.time(),time.time()+86400*11)
            busy+=[{'start':datetime.fromisoformat(b['start'].replace('Z','+00:00')).timestamp(),'end':datetime.fromisoformat(b['end'].replace('Z','+00:00')).timestamp()} for b in result['busy']]
        return {'business':workspace.name,'name':contact.name,'timezone':workspace.timezone,'duration_minutes':30,'slots':self._slots(workspace,campaign,busy),'calendar_connected':external}

    def reserve(self,wid,cid,token,starts_at):
        with self.factory() as db:
            self._records(db,wid,cid,token)
            existing=db.scalar(select(Meeting).where(Meeting.workspace_id==wid,Meeting.contact_id==cid,Meeting.starts_at==starts_at,Meeting.status=='confirmed'))
            if existing:return {'id':existing.id,'status':'confirmed','starts_at':existing.starts_at}
        available=self.availability(wid,cid,token)
        if starts_at not in available['slots']:raise BookingError('Ese horario ya no está disponible. Elegí otro.')
        with self.factory() as db:
            if db.bind.dialect.name=='sqlite':db.execute(text('BEGIN IMMEDIATE'))
            else:db.execute(select(Workspace.id).where(Workspace.id==wid).with_for_update())
            self._records(db,wid,cid,token)
            overlap=db.scalar(select(Meeting.id).where(Meeting.workspace_id==wid,Meeting.status.in_(['confirmed','reserving']),Meeting.starts_at<starts_at+1800,(Meeting.starts_at+Meeting.duration_minutes*60)>starts_at))
            if overlap:raise BookingError('Otra persona reservó ese horario. Elegí otro.')
            active=db.scalar(select(Meeting.id).where(Meeting.workspace_id==wid,Meeting.contact_id==cid,Meeting.status.in_(['confirmed','reserving']),Meeting.starts_at>time.time()))
            if active:raise BookingError('Ya tenés una reunión reservada. Contactá al equipo para cambiarla.')
            row=Meeting(workspace_id=wid,contact_id=cid,starts_at=starts_at,duration_minutes=30,status='reserving',notes='Horario aceptado por el contacto mediante su enlace privado.')
            db.add(row);db.commit();identity=row.id
        try:
            if available['calendar_connected']:
                from .calendar_service import CalendarService
                CalendarService(self.factory,self.vault).create_event(wid,identity)
            with self.factory() as db:
                row=db.get(Meeting,identity);row.status='confirmed'
                db.add(Event(workspace_id=wid,kind='meeting_booked',detail=f'El contacto {cid} reservó una reunión para {datetime.fromtimestamp(starts_at,ZoneInfo(available["timezone"])).strftime("%d/%m %H:%M")}.'));db.commit()
            return {'id':identity,'status':'confirmed','starts_at':starts_at}
        except Exception:
            # Keep a pending reservation on ambiguous external failure. Do not
            # reopen the slot until the owner checks the external calendar.
            with self.factory() as db:
                db.add(Event(workspace_id=wid,kind='booking_needs_review',detail=f'La reserva {identity} requiere comprobar el calendario.'));db.commit()
            raise BookingError('Estamos verificando la reserva con el calendario. El equipo revisará su confirmación.')
