"""Standalone worker; explicit invocation is required. Never started by the API."""
import logging
import re
import signal
import time
from sqlalchemy import select
from .models import Account, Campaign, Contact, Event, Job, Message, Suppression, Workspace
from .security import Vault
from .settings import Settings
from .store import make_store

log = logging.getLogger('egasis.worker')

CLASSIFICATION_REVIEW_REASON = 'La clasificación de la conversación o del borrador requiere revisión antes de responder automáticamente.'

def tick(factory,vault,settings,workspace_id=None):
    from .mail import MailEngine
    from .intelligence import Intelligence
    mail=MailEngine(factory,vault,settings.simulation,settings.public_url)
    with factory() as db:
        stmt=select(Campaign).where(Campaign.status=='active')
        if workspace_id is not None: stmt=stmt.where(Campaign.workspace_id==workspace_id)
        campaigns=db.scalars(stmt).all()
    prepared=0
    for campaign in campaigns:
        try:
            with factory() as db:
                workspace=db.get(Workspace,campaign.workspace_id)
                if workspace.subscription_status not in {'pilot','active','trialing'}:continue
            prepared+=mail.prepare_campaign(campaign.workspace_id,campaign.id)
        except ValueError:
            with factory() as db:
                row=db.get(Campaign,campaign.id)
                row.status='paused'
                db.add(Event(workspace_id=campaign.workspace_id,kind='campaign_needs_review',detail=f'La campaña {campaign.id} se pausó: revisá su cuenta y plantilla.'));db.commit()
    # Automatic replies require explicit campaign policy and a successful AI draft.
    with factory() as db:
        stmt=select(Message).join(Contact,Message.contact_id==Contact.id).join(Campaign,Contact.campaign_id==Campaign.id).where(Message.direction=='inbound',Message.status=='received',Message.classification.notin_(['unsubscribe','bounce','out_of_office']),Campaign.reply_mode=='automatic',Campaign.status=='active')
        if workspace_id is not None: stmt=stmt.where(Message.workspace_id==workspace_id)
        inbound=db.scalars(stmt.limit(10)).all()
    for message in inbound:
        try:
            with factory() as db:
                original=db.get(Message,message.id)
                contact=db.get(Contact,message.contact_id)
                campaign=db.get(Campaign,contact.campaign_id)
                workspace=db.get(Workspace,message.workspace_id)
                existing=db.scalar(select(Message).where(Message.workspace_id==message.workspace_id,Message.idempotency_key==f'reply:{message.id}'))
                if existing and existing.status in {'queued','sending','sent','simulated','uncertain'}:
                    original.status='processed';original.error='';db.commit();continue
                variables=set(re.findall(r'\{\{(.*?)\}\}',campaign.auto_reply_body or ''))
                review_reason=''
                if not (campaign.auto_reply_body or '').strip():
                    review_reason='Falta configurar la plantilla aprobada para las respuestas automáticas.'
                elif variables-{'name','company','offer','signature','booking_link'}:
                    review_reason='La plantilla de respuesta automática contiene variables no permitidas.'
                elif workspace.subscription_status not in {'pilot','active','trialing'}:
                    review_reason='La suscripción del espacio no permite preparar respuestas automáticas.'
                if review_reason:
                    original.error=review_reason
                    original.status='needs_review';db.commit();continue
            draft=Intelligence(factory,vault).draft_reply(message.workspace_id,message.contact_id,message.id)
            with factory() as db:
                row=db.get(Message,draft.id) if draft else None
                original=db.get(Message,message.id)
                if row and row.status=='draft':
                    contact=db.get(Contact,row.contact_id)
                    campaign=db.get(Campaign,contact.campaign_id)
                    workspace=db.get(Workspace,row.workspace_id)
                    if original.classification not in {'interested','question'} or row.classification not in {'interested','question'}:
                        original.error=CLASSIFICATION_REVIEW_REASON
                        original.status='needs_review';db.commit();continue
                    if not campaign.auto_reply_body.strip():
                        original.error='Falta configurar la plantilla aprobada para las respuestas automáticas.'
                        original.status='needs_review';db.commit();continue
                    values={'name':contact.name or 'equipo','company':contact.company,'offer':campaign.offer or workspace.offer,'signature':workspace.signature}
                    from .booking import booking_link
                    values['booking_link']=booking_link(settings,vault,message.workspace_id,contact.id)
                    body=campaign.auto_reply_body
                    for key,value in values.items(): body=body.replace('{{'+key+'}}',value)
                    row.body=body
                    row.status='queued'
                    row.error=''
                    if not db.scalar(select(Job.id).where(Job.message_id==row.id)):
                        db.add(Job(workspace_id=row.workspace_id,message_id=row.id))
                    original.status='processed';original.error='';db.commit()
                elif original:
                    original.error='No hay un borrador disponible para preparar la respuesta automática.'
                    original.status='needs_review';db.commit()
        except Exception:
            # Leave visible for review; never repeatedly spend on the same unavailable action.
            with factory() as db:
                original=db.get(Message,message.id)
                if original:
                    original.status='needs_review'
                    original.error=(CLASSIFICATION_REVIEW_REASON if original.classification in {'unsubscribe','not_interested','out_of_office','bounce','unknown','objection'}
                                    else 'No se pudo preparar la respuesta automática. Revisá la conversación antes de continuar.')
                db.add(Event(workspace_id=message.workspace_id,kind='reply_needs_review',detail=f'La respuesta {message.id} necesita revisión. No se envió ningún correo.'));db.commit()
    if workspace_id is not None:
        # Interactive simulation must not execute another tenant's work.
        return {'prepared':prepared,'result':mail.run_once(workspace_id=workspace_id,
            synchronize_inbox=not settings.simulation)}
    return {'prepared':prepared,'result':mail.run_once(synchronize_inbox=not settings.simulation)}

def main():
    settings=Settings.from_env();_,factory=make_store(settings.database_url);vault=Vault(settings.secret_key)
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    last_sync=0
    while not stopping:
        try:
            if not settings.simulation and time.time()-last_sync>120:
                from .mail import MailEngine
                mail=MailEngine(factory,vault,False,settings.public_url)
                with factory() as db: accounts=db.scalars(select(Account).where(Account.active==True)).all()
                for account in accounts:
                    if stopping:break
                    try:
                        mail.sync_account(account.workspace_id,account.id)
                    except Exception:
                        log.warning('Inbox sync failed for account %s; continuing other accounts',account.id)
                last_sync=time.time()
            tick(factory,vault,settings)
        except Exception:
            log.exception('Worker cycle failed')
        for _ in range(10):
            if stopping:break
            time.sleep(1)

if __name__=='__main__':main()
