from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage
import imaplib
import smtplib
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select

from egasis.mail import MailEngine
from egasis.models import Account, Campaign, Contact, Event, Job, Message, Suppression, Workspace
from egasis.security import Vault
from egasis.store import make_store


NOW = datetime(2026, 9, 8, 15, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def no_real_mail(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Tests cannot connect to real email services')
    monkeypatch.setattr(smtplib, 'SMTP', forbidden)
    monkeypatch.setattr(smtplib, 'SMTP_SSL', forbidden)
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', forbidden)


@pytest.fixture
def setup(tmp_path):
    engine, sessions = make_store('sqlite:///' + str(tmp_path / 'mail-tests.sqlite'))
    vault = Vault('isolated-test-secret')
    with sessions() as db:
        workspace = Workspace(name='Consultoría de logística', offer='Planificación logística', signature='Laura · Operaciones')
        db.add(workspace)
        db.flush()
        account = Account(workspace_id=workspace.id, email='laura@seller-business.com', secret=vault.encrypt('fake-password'), daily_limit=20, cooldown_seconds=0)
        campaign = Campaign(workspace_id=workspace.id, name='Planificación', subject='Una idea para {{company}}', body='Hola {{name}}. {{offer}}\n{{signature}}', status='active', weekdays='0,1,2,3,4,5,6', start_hour=0, end_hour=24, daily_limit=20)
        db.add_all([account, campaign])
        db.flush()
        contact = Contact(workspace_id=workspace.id, campaign_id=campaign.id, email='ana@buyer-business.com', name='Ana', company='Mercado Sur')
        db.add(contact)
        db.commit()
        ids = (workspace.id, account.id, campaign.id, contact.id)
    mail = MailEngine(sessions, vault, public_url='https://app.egasis.com')
    mail.clock = lambda: NOW
    yield mail, sessions, ids
    engine.dispose()


def first_message(sessions):
    with sessions() as db:
        return db.scalar(select(Message).where(Message.direction == 'outbound').order_by(Message.id))


def add_contact(sessions, ids, email='second@buyer-business.com'):
    workspace_id, _, campaign_id, _ = ids
    with sessions() as db:
        row = Contact(workspace_id=workspace_id, campaign_id=campaign_id, email=email, name='José', company='Otra empresa')
        db.add(row)
        db.commit()
        return row.id


def test_configured_first_email_is_rendered_once_and_pins_sender(setup):
    mail, sessions, (wid, aid, cid, contact_id) = setup
    assert mail.prepare_campaign(wid, cid) == 1
    assert mail.prepare_campaign(wid, cid) == 0
    outbound = first_message(sessions)
    assert outbound.subject == 'Una idea para Mercado Sur'
    assert outbound.body == 'Hola Ana. Planificación logística\nLaura · Operaciones'
    assert outbound.account_id == aid
    with sessions() as db:
        assert db.get(Contact, contact_id).account_id == aid
        assert db.scalar(select(func.count(Job.id))) == 1


def test_booking_link_is_scoped_signed_and_does_not_confirm_a_meeting(setup):
    from egasis.models import Meeting
    mail, sessions, (wid, _, cid, contact_id) = setup
    with sessions() as db:
        db.get(Campaign, cid).body = 'Hola {{name}}. Elegí un horario: {{booking_link}}'
        db.commit()
    mail.prepare_campaign(wid, cid)
    body = first_message(sessions).body
    url = body.split('Elegí un horario: ')[1]
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    assert parsed.path == '/book' and parsed.netloc == 'app.egasis.com'
    assert params['workspace_id'] == [str(wid)] and params['contact_id'] == [str(contact_id)]
    assert mail.vault.verify_booking(wid, contact_id, params['token'][0])
    assert not mail.vault.verify_booking(wid + 1, contact_id, params['token'][0])
    with sessions() as db:
        assert db.scalar(select(func.count(Meeting.id))) == 0


def test_stopped_unattempted_first_message_rebuilds_in_place_with_new_template(setup):
    mail, sessions, (wid, _, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    original = first_message(sessions)
    with sessions() as db:
        db.get(Message, original.id).status = 'cancelled'
        db.scalar(select(Job)).status = 'cancelled'
        db.get(Campaign, cid).body = 'Nueva oferta para {{company}}'
        db.commit()
    assert mail.prepare_campaign(wid, cid) == 1
    changed = first_message(sessions)
    assert changed.id == original.id and changed.body == 'Nueva oferta para Mercado Sur'
    assert mail.prepare_campaign(wid, cid) == 0
    with sessions() as db:
        assert db.scalar(select(func.count(Job.id))) == 1
        assert db.scalar(select(Job)).status == 'pending'


@pytest.mark.parametrize('risk', ['attempted', 'reserved', 'provider_id', 'job_running', 'wrong_account', 'inbound'])
def test_cancelled_first_message_with_any_delivery_or_conversation_risk_never_rebuilds(setup, risk):
    mail, sessions, (wid, aid, cid, contact_id) = setup
    mail.prepare_campaign(wid, cid)
    with sessions() as db:
        message, job = db.scalar(select(Message)), db.scalar(select(Job))
        message.status = job.status = 'cancelled'
        if risk == 'attempted': job.attempts = 1
        if risk == 'reserved': job.reserved_at = NOW
        if risk == 'provider_id': message.provider_id = '<possible-send@seller-business.com>'
        if risk == 'job_running': job.status = 'running'
        if risk == 'wrong_account': message.account_id = None
        if risk == 'inbound':
            db.add(Message(workspace_id=wid, contact_id=contact_id, account_id=aid, direction='inbound',
                subject='Re', body='No volver a preparar', status='received', provider_id='<earlier-reply>'))
        db.commit()
    assert mail.prepare_campaign(wid, cid) == 0
    assert first_message(sessions).status == 'cancelled'


@pytest.mark.parametrize('failure', ['error', 'backlog'])
def test_required_inbox_sync_failure_keeps_real_reply_unclaimed(setup, failure):
    mail, sessions, _ = setup
    reply_id = queue_reply(setup)
    mail.simulation = False
    mail._deliver = lambda *_: pytest.fail('An unverified inbox reached SMTP')
    if failure == 'error':
        mail.sync_account = lambda *_: (_ for _ in ()).throw(OSError('IMAP unavailable'))
    else:
        mail.sync_account = lambda *_: {'imported': 200, 'has_more': True}
    assert mail.run_once(synchronize_inbox=True) == {'status': 'blocked', 'reason': 'inbox_sync', 'blocked_accounts': 1}
    with sessions() as db:
        job = db.scalar(select(Job).where(Job.message_id == reply_id))
        assert job.status == 'pending' and job.attempts == 0
        assert db.get(Message, reply_id).status == 'queued'


def test_pre_send_sync_ingests_new_reply_before_claim_and_cancels_old_response(setup):
    mail, sessions, (wid, aid, _, _) = setup
    reply_id = queue_reply(setup)
    mail.simulation = False
    mail._deliver = lambda *_: pytest.fail('The old response reached SMTP')
    def sync(*_):
        mail.ingest(wid, aid, '<newest-at-sync>', 'ana@buyer-business.com', 'Re', 'Hay un cambio antes de avanzar.')
        return {'imported': 1, 'duplicates': 0, 'ignored': 0}
    mail.sync_account = sync
    assert mail.run_once(synchronize_inbox=True)['status'] == 'idle'
    with sessions() as db:
        assert db.get(Message, reply_id).status == 'cancelled'
        assert db.scalar(select(Job).where(Job.message_id == reply_id)).attempts == 0


def test_simulation_is_never_counted_as_sent(setup):
    mail, sessions, (wid, _, cid, contact_id) = setup
    mail.prepare_campaign(wid, cid)
    assert mail.run_once()['status'] == 'simulated'
    outbound = first_message(sessions)
    assert outbound.status == 'simulated'
    assert outbound.sent_at is None
    with sessions() as db:
        assert db.get(Contact, contact_id).status == 'simulated'
        assert db.scalar(select(Job)).status == 'done'


def test_successful_smtp_acceptance_updates_only_after_delivery(setup):
    mail, sessions, (wid, aid, cid, _) = setup
    mail.simulation = False
    mail.prepare_campaign(wid, cid)
    observed = []
    def deliver(account, recipient, message):
        with sessions() as db:
            row = db.scalar(select(Message))
            assert row.status == 'sending' and row.sent_at is None
            assert db.scalar(select(Job)).attempts == 1
        assert account.id == aid
        assert mail.vault.decrypt(account.secret) == 'fake-password'
        observed.append(message)
    mail._deliver = deliver
    assert mail.run_once()['status'] == 'sent'
    outbound = first_message(sessions)
    assert outbound.sent_at == NOW
    assert observed[0]['Message-ID'] == outbound.provider_id
    assert '/api/unsubscribe?workspace_id=' in observed[0]['List-Unsubscribe']
    assert observed[0]['List-Unsubscribe-Post'] == 'List-Unsubscribe=One-Click'


def test_connection_failure_retries_without_fake_sent_status(setup):
    mail, sessions, (wid, _, cid, _) = setup
    mail.simulation = False
    mail.prepare_campaign(wid, cid)
    mail._deliver = lambda *_: (_ for _ in ()).throw(ConnectionRefusedError())
    assert mail.run_once()['status'] == 'failed'
    with sessions() as db:
        message, job = db.scalar(select(Message)), db.scalar(select(Job))
        assert message.status == 'queued' and message.sent_at is None
        assert job.status == 'pending' and job.attempts == 1
        assert job.due_at == NOW + 60
    assert mail.run_once()['status'] == 'idle'
    mail.clock = lambda: NOW + 60
    mail.run_once()
    mail.clock = lambda: NOW + 180
    mail.run_once()
    with sessions() as db:
        assert db.scalar(select(Job)).status == 'failed'
        assert db.scalar(select(Job)).attempts == 3
        assert db.scalar(select(Message)).status == 'failed'
        assert db.scalar(select(func.count(Event.id)).where(Event.kind == 'mail.attempt')) == 3


class FakeSMTP:
    def __init__(self, data_error=None, data_code=250, auth_error=None):
        self.data_error, self.data_code, self.auth_error = data_error, data_code, auth_error
        self.auth = None
        self.deliveries = []

    def login(self, email, password):
        self.auth = (email, password)
        if self.auth_error:
            raise self.auth_error

    def mail(self, sender):
        return 250, b'OK'

    def rcpt(self, recipient):
        return 250, b'OK'

    def data(self, data):
        self.deliveries.append(data)
        if self.data_error:
            raise self.data_error
        return self.data_code, b'result'

    def quit(self):
        return 221, b'Bye'

    def close(self):
        pass


@pytest.mark.parametrize('error', [TimeoutError(), smtplib.SMTPServerDisconnected('lost ack')])
def test_data_disconnect_is_uncertain_and_never_retried(setup, error):
    mail, sessions, (wid, _, cid, _) = setup
    mail.simulation = False
    mail.prepare_campaign(wid, cid)
    smtp = FakeSMTP(data_error=error)
    mail._smtp_open = lambda _: smtp
    assert mail.run_once()['status'] == 'uncertain'
    mail.clock = lambda: NOW + 86_400
    assert mail.run_once()['status'] == 'idle'
    assert len(smtp.deliveries) == 1
    assert smtp.auth == ('laura@seller-business.com', 'fake-password')
    with sessions() as db:
        assert db.scalar(select(Job)).status == 'uncertain'
        assert db.scalar(select(Message)).sent_at is None


def test_explicit_data_rejection_is_failure_and_auth_failure_is_permanent(setup):
    mail, sessions, (wid, _, cid, _) = setup
    mail.simulation = False
    mail.prepare_campaign(wid, cid)
    mail._smtp_open = lambda _: FakeSMTP(data_code=451)
    assert mail.run_once()['status'] == 'failed'
    with sessions() as db:
        assert db.scalar(select(Job)).status == 'pending'
    mail.clock = lambda: NOW + 60
    mail._smtp_open = lambda _: FakeSMTP(auth_error=smtplib.SMTPAuthenticationError(535, b'bad credentials'))
    assert mail.run_once()['status'] == 'failed'
    with sessions() as db:
        assert db.scalar(select(Job)).status == 'failed'


def test_quit_failure_does_not_turn_accepted_message_into_retry(setup):
    mail, sessions, (wid, _, cid, _) = setup
    mail.simulation = False
    mail.prepare_campaign(wid, cid)
    smtp = FakeSMTP()
    smtp.quit = lambda: (_ for _ in ()).throw(OSError())
    smtp.close = lambda: (_ for _ in ()).throw(OSError())
    mail._smtp_open = lambda _: smtp
    assert mail.run_once()['status'] == 'sent'


def test_expired_claim_is_held_uncertain_after_worker_restart(setup):
    mail, sessions, (wid, _, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    with sessions() as db:
        jid = db.scalar(select(Job.id))
    assert mail._claim(jid, wid, NOW)
    restarted = MailEngine(sessions, mail.vault)
    restarted.clock = lambda: NOW + mail.LEASE_SECONDS + 1
    assert restarted.run_once()['status'] == 'idle'
    with sessions() as db:
        assert db.get(Job, jid).status == 'uncertain'
        assert db.scalar(select(Message)).status == 'uncertain'


@pytest.mark.parametrize('constraint', ['account', 'campaign'])
def test_concurrent_workers_reserve_limits_atomically(setup, constraint):
    mail, sessions, ids = setup
    wid, aid, cid, _ = ids
    add_contact(sessions, ids)
    with sessions() as db:
        if constraint == 'account':
            db.get(Account, aid).daily_limit = 1
        else:
            db.get(Campaign, cid).daily_limit = 1
        db.commit()
    mail.simulation = False
    mail.prepare_campaign(wid, cid)
    entered, release = threading.Event(), threading.Event()
    def deliver(*_):
        entered.set()
        assert release.wait(5)
    mail._deliver = deliver
    second = MailEngine(sessions, mail.vault, False, 'https://app.egasis.com')
    second.clock = lambda: NOW
    second._deliver = lambda *_: pytest.fail('Concurrent quota was exceeded')
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(mail.run_once)
        assert entered.wait(5)
        try:
            assert second.run_once()['status'] == 'idle'
        finally:
            release.set()
        assert pending.result()['status'] == 'sent'
    with sessions() as db:
        assert db.scalar(select(func.count(Message.id)).where(Message.status == 'sent')) == 1


def test_campaign_schedule_pause_cooldown_and_next_local_day(setup):
    mail, sessions, ids = setup
    wid, aid, cid, _ = ids
    add_contact(sessions, ids)
    mail.prepare_campaign(wid, cid)
    with sessions() as db:
        campaign = db.get(Campaign, cid)
        campaign.status = 'paused'
        db.commit()
    assert mail.run_once()['status'] == 'idle'
    with sessions() as db:
        campaign = db.get(Campaign, cid)
        campaign.status, campaign.start_hour, campaign.end_hour = 'active', 9, 11
        db.commit()
    assert mail.run_once()['status'] == 'idle'  # Noon in Buenos Aires.
    with sessions() as db:
        db.get(Campaign, cid).end_hour = 18
        db.get(Account, aid).cooldown_seconds = 120
        db.commit()
    assert mail.run_once()['status'] == 'simulated'
    assert mail.run_once()['status'] == 'idle'
    mail.clock = lambda: NOW + 121
    assert mail.run_once()['status'] == 'simulated'


def test_daily_quota_resets_at_workspace_midnight(setup):
    mail, sessions, ids = setup
    wid, aid, cid, _ = ids
    add_contact(sessions, ids)
    with sessions() as db:
        db.get(Account, aid).daily_limit = 1
        db.commit()
    mail.prepare_campaign(wid, cid)
    assert mail.run_once()['status'] == 'simulated'
    mail.clock = lambda: datetime(2026, 9, 9, 2, 59, tzinfo=timezone.utc).timestamp()
    assert mail.run_once()['status'] == 'idle'  # 23:59 on the same local day.
    mail.clock = lambda: datetime(2026, 9, 9, 3, 0, tzinfo=timezone.utc).timestamp()
    assert mail.run_once()['status'] == 'simulated'


def test_incoming_reply_between_claim_and_transport_cancels_first_email(setup):
    mail, sessions, (wid, aid, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    original_claim = mail._claim
    def claim_then_reply(*args):
        payload = original_claim(*args)
        if payload:
            mail.ingest(wid, aid, '<just-arrived>', 'ana@buyer-business.com', 'Re', 'Hola, antes de continuar...')
        return payload
    mail._claim = claim_then_reply
    assert mail.run_once()['status'] == 'cancelled'
    with sessions() as db:
        assert db.scalar(select(Job)).status == 'cancelled'


def test_reply_stops_first_message_and_same_subject_different_ids_survive(setup):
    mail, sessions, (wid, aid, cid, contact_id) = setup
    mail.prepare_campaign(wid, cid)
    first = mail.ingest(wid, aid, '<incoming-1@buyer>', 'Ana <ana@buyer-business.com>', 'Re: Propuesta', 'Me interesa')
    second = mail.ingest(wid, aid, '<incoming-2@buyer>', 'ana@buyer-business.com', 'Re: Propuesta', '¿Cuánto cuesta?')
    duplicate = mail.ingest(wid, aid, '<incoming-2@buyer>', 'ana@buyer-business.com', 'Re: Propuesta', '¿Cuánto cuesta?')
    assert first.id != second.id and duplicate.id == second.id
    assert duplicate._egasis_duplicate
    with sessions() as db:
        assert db.get(Contact, contact_id).status == 'replied'
        assert db.scalar(select(Job)).status == 'cancelled'
        assert db.scalar(select(func.count(Message.id)).where(Message.direction == 'inbound')) == 2
    assert mail.run_once()['status'] == 'idle'


def test_ambiguous_incoming_requires_thread_id(setup):
    mail, sessions, (wid, aid, cid, contact_id) = setup
    mail.prepare_campaign(wid, cid)
    mail.run_once()
    outbound = first_message(sessions)
    with sessions() as db:
        other_campaign = Campaign(workspace_id=wid, name='Other', subject='Other', body='Other')
        db.add(other_campaign)
        db.flush()
        db.add(Contact(workspace_id=wid, campaign_id=other_campaign.id, account_id=aid, email='ana@buyer-business.com'))
        db.commit()
    assert mail.ingest(wid, aid, '<ambiguous>', 'ana@buyer-business.com', 'Re', 'Hello') is None
    assert mail.ingest(wid, aid, '<unknown>', 'unknown@buyer-business.com', 'Re', 'Hello') is None
    reply = mail.ingest(wid, aid, '<clear>', 'ana@buyer-business.com', 'Re', 'Hello', outbound.provider_id)
    assert reply.contact_id == contact_id


def test_unsubscribe_suppresses_across_campaigns_and_honors_quoted_text(setup):
    mail, sessions, (wid, aid, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    reply = mail.ingest(wid, aid, '<quote>', 'ana@buyer-business.com', 'Re', 'Sí, me interesa\n> unsubscribe')
    assert reply.classification == 'unclassified'
    reply = mail.ingest(wid, aid, '<optout>', 'ana@buyer-business.com', 'Re', 'Por favor, no me escriban más.')
    assert reply.classification == 'unsubscribe'
    with sessions() as db:
        assert db.scalar(select(Suppression)).email == 'ana@buyer-business.com'
    later = mail.ingest(wid, aid, '<later>', 'ana@buyer-business.com', 'Re', 'Gracias')
    with sessions() as db:
        assert db.get(Contact, later.contact_id).status == 'do_not_contact'


def test_baja_reply_honors_the_published_unsubscribe_instruction(setup):
    mail, sessions, (wid, aid, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    assert mail.ingest(wid, aid, '<baja>', 'ana@buyer-business.com', 'Re', 'BAJA').classification == 'unsubscribe'
    with sessions() as db:
        assert db.scalar(select(Job)).status == 'cancelled'


def test_bounce_for_an_unsent_contact_is_ignored(setup):
    mail, _, (wid, aid, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    body = 'Final-Recipient: rfc822; ana@buyer-business.com\nStatus: 5.1.1'
    assert mail.ingest(wid, aid, '<unsent-bounce>', 'mailer-daemon@mx.com', 'Failure', body) is None


def test_only_structured_permanent_bounce_suppresses(setup):
    mail, sessions, (wid, aid, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    mail.run_once()
    outbound = first_message(sessions)
    assert mail.ingest(wid, aid, '<fake-bounce>', 'mailer-daemon@mx.com', 'Failure', 'Delivery temporarily delayed', outbound.provider_id) is None
    bounce = mail.ingest(wid, aid, '<real-bounce>', 'mailer-daemon@mx.com', 'Failure', 'Final-Recipient: rfc822; ana@buyer-business.com\nStatus: 5.1.1', outbound.provider_id)
    assert bounce.classification == 'bounce'
    with sessions() as db:
        assert db.scalar(select(Suppression)).reason == 'bounce'


def test_reply_stays_on_original_sender_and_has_thread_headers(setup):
    mail, sessions, (wid, aid, cid, contact_id) = setup
    mail.prepare_campaign(wid, cid)
    mail.run_once()
    initial = first_message(sessions)
    incoming = mail.ingest(wid, aid, '<reply-from-buyer>', 'ana@buyer-business.com', 'Re', 'Más información', initial.provider_id)
    with sessions() as db:
        message = Message(workspace_id=wid, contact_id=contact_id, account_id=aid, direction='outbound', subject='Re', body='Oferta configurada', status='queued', in_reply_to=incoming.provider_id, idempotency_key=f'reply:{incoming.id}')
        db.add(message)
        db.flush()
        db.add(Job(workspace_id=wid, message_id=message.id, due_at=NOW))
        db.commit()
    mail.simulation = False
    captured = []
    mail._deliver = lambda account, recipient, message: captured.append((account.id, message))
    assert mail.run_once()['status'] == 'sent'
    assert captured[0][0] == aid
    assert captured[0][1]['In-Reply-To'] == '<reply-from-buyer>'
    assert initial.provider_id in captured[0][1]['References']


def test_real_mode_rejects_demo_and_public_url_missing(setup):
    mail, sessions, (wid, _, cid, contact_id) = setup
    mail.prepare_campaign(wid, cid)
    mail.simulation = False
    with sessions() as db:
        db.get(Contact, contact_id).source = 'demo'
        db.commit()
    assert mail.run_once()['status'] == 'idle'
    with sessions() as db:
        assert db.scalar(select(Job)).status == 'cancelled'


def test_workspace_scope_prevents_cross_tenant_claim_and_association(setup):
    mail, sessions, (wid, aid, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    with sessions() as db:
        other = Workspace(name='Otra organización')
        db.add(other)
        db.commit()
        other_id = other.id
    assert mail.run_once(workspace_id=other_id)['status'] == 'idle'
    with pytest.raises(ValueError):
        mail.prepare_campaign(other_id, cid)
    with pytest.raises(ValueError):
        mail.ingest(other_id, aid, '<cross-tenant>', 'ana@buyer-business.com', 'Re', 'Hello')
    assert mail.run_once(workspace_id=wid)['status'] == 'simulated'


def queue_reply(setup):
    mail, sessions, (wid, aid, cid, contact_id) = setup
    mail.prepare_campaign(wid, cid)
    mail.run_once()
    initial = first_message(sessions)
    incoming = mail.ingest(wid, aid, '<first-inbound>', 'ana@buyer-business.com', 'Re', '¿Podemos conversar?', initial.provider_id)
    with sessions() as db:
        reply = Message(workspace_id=wid, contact_id=contact_id, account_id=aid,
            direction='outbound', subject='Re', body='Sí, podemos coordinar.', status='queued',
            in_reply_to=incoming.provider_id, idempotency_key=f'reply:{incoming.id}')
        db.add(reply)
        db.flush()
        db.add(Job(workspace_id=wid, message_id=reply.id, due_at=NOW))
        db.commit()
        return reply.id


def test_queued_reply_to_old_inbound_is_cancelled_before_claim(setup):
    mail, sessions, (wid, aid, _, _) = setup
    reply_id = queue_reply(setup)
    mail.ingest(wid, aid, '<newer-inbound>', 'ana@buyer-business.com', 'Re', 'Cambio de planes; la semana próxima.')
    assert mail.run_once()['status'] == 'idle'
    with sessions() as db:
        assert db.get(Message, reply_id).status == 'cancelled'
        job = db.scalar(select(Job).where(Job.message_id == reply_id))
        assert job.status == 'cancelled' and job.attempts == 0


def test_newer_inbound_after_reply_claim_is_checked_again_before_smtp(setup):
    mail, sessions, (wid, aid, _, _) = setup
    reply_id = queue_reply(setup)
    claim = mail._claim
    def claim_then_receive(*args):
        payload = claim(*args)
        if payload:
            mail.ingest(wid, aid, '<newest-inbound>', 'ana@buyer-business.com', 'Re', 'Otra consulta antes de coordinar.')
        return payload
    mail._claim = claim_then_receive
    mail.simulation = False
    mail._deliver = lambda *_: pytest.fail('A stale reply reached SMTP')
    assert mail.run_once()['status'] == 'cancelled'
    with sessions() as db:
        assert db.get(Message, reply_id).status == 'cancelled'
        assert db.get(Message, reply_id).sent_at is None


@pytest.mark.parametrize('status', ['past_due', 'canceled', 'unpaid', 'incomplete', 'paused'])
def test_subscription_change_blocks_existing_active_campaign(setup, status):
    mail, sessions, (wid, _, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    with sessions() as db:
        db.get(Workspace, wid).subscription_status = status
        db.commit()
    assert mail.run_once()['status'] == 'idle'
    with sessions() as db:
        job = db.scalar(select(Job))
        assert job.attempts == 0 and job.status == 'pending'
        assert job.error == 'Subscription does not allow sending'
        db.get(Workspace, wid).subscription_status = 'active'
        db.commit()
    assert mail.run_once()['status'] == 'simulated'


def test_subscription_is_rechecked_between_claim_and_smtp(setup):
    mail, sessions, (wid, _, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    claim = mail._claim
    def claim_then_cancel_subscription(*args):
        payload = claim(*args)
        if payload:
            with sessions() as db:
                db.get(Workspace, wid).subscription_status = 'canceled'
                db.commit()
        return payload
    mail._claim = claim_then_cancel_subscription
    mail.simulation = False
    mail._deliver = lambda *_: pytest.fail('Subscription was not rechecked')
    assert mail.run_once()['status'] == 'blocked'
    with sessions() as db:
        assert db.scalar(select(Job)).status == 'pending'
        assert db.scalar(select(Message)).status == 'queued'


def uncertain_message(setup):
    mail, sessions, (wid, _, cid, _) = setup
    mail.simulation = False
    mail.prepare_campaign(wid, cid)
    mail._smtp_open = lambda _: FakeSMTP(data_error=TimeoutError())
    assert mail.run_once()['status'] == 'uncertain'
    return first_message(sessions).id


@pytest.mark.parametrize('outcome,expected', [('accepted', 'sent'), ('not_sent', 'failed')])
def test_uncertain_reconciliation_has_evidence_and_does_not_retry(setup, outcome, expected):
    mail, sessions, (wid, _, _, _) = setup
    mid = uncertain_message(setup)
    mail.clock = lambda: NOW + 10
    result = mail.reconcile(wid, mid, outcome, 'Operator checked the provider SMTP audit log.', accepted_at=NOW + 1 if outcome == 'accepted' else None)
    assert set(result) == {'status', 'job_id', 'message_id', 'error'}
    assert result['status'] == expected and result['message_id'] == mid
    mail.clock = lambda: NOW + 1000
    assert mail.run_once()['status'] == 'idle'
    with sessions() as db:
        assert db.get(Message, mid).status == expected
        assert db.scalar(select(Event).where(Event.kind == 'mail.reconciled')) is not None
        assert db.get(Message, mid).sent_at == (NOW + 1 if outcome == 'accepted' else None)
    with pytest.raises(ValueError, match='Only an uncertain'):
        mail.reconcile(wid, mid, outcome, 'Operator checked the provider SMTP audit log.', accepted_at=NOW + 1 if outcome == 'accepted' else None)


def test_reconciliation_rejects_wrong_workspace_missing_or_invalid_evidence(setup):
    mail, sessions, (wid, _, _, _) = setup
    mid = uncertain_message(setup)
    with sessions() as db:
        other = Workspace(name='Other workspace')
        db.add(other)
        db.commit()
        other_id = other.id
    evidence = 'Provider SMTP logs reviewed by workspace owner.'
    with pytest.raises(ValueError, match='does not belong'):
        mail.reconcile(other_id, mid, 'not_sent', evidence)
    for stamp in (None, float('nan'), float('inf'), NOW + 1, True, NOW - 1):
        with pytest.raises(ValueError):
            mail.reconcile(wid, mid, 'accepted', evidence, stamp)
    with pytest.raises(ValueError, match='evidence'):
        mail.reconcile(wid, mid, 'not_sent', 'maybe')
    with pytest.raises(ValueError, match='outcome'):
        mail.reconcile(wid, mid, 'retry', evidence)
    with sessions() as db:
        assert db.get(Message, mid).status == 'uncertain'


class FakeIMAP:
    def __init__(self, records, validity='10'):
        self.records, self.validity, self.searches, self.auth = records, validity, [], None

    def login(self, email, password):
        self.auth = (email, password)

    def select(self, folder, readonly=False):
        assert readonly
        return 'OK', [str(len(self.records)).encode()]

    def response(self, key):
        assert key == 'UIDVALIDITY'
        return key, [self.validity.encode()]

    def uid(self, command, *args):
        if command == 'search':
            self.searches.append(args[-1])
            return 'OK', [' '.join(str(uid) for uid in self.records).encode()]
        if command == 'fetch':
            return 'OK', [(b'UID', self.records[int(args[0])])]
        raise AssertionError(command)

    def logout(self):
        pass


def test_imap_decrypts_uses_uid_cursor_and_parses_html_and_ids(setup):
    mail, sessions, (wid, aid, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    records = {}
    for uid in (1, 2):
        message = EmailMessage()
        message['From'], message['Subject'] = 'Ana <ana@buyer-business.com>', 'Re: Misma propuesta'
        message['Message-ID'] = f'<incoming-{uid}@buyer>'
        message.set_content(f'<p>Respuesta {uid}</p><script>bad()</script><p>Gracias</p>', subtype='html')
        records[uid] = message.as_bytes()
    mailbox = FakeIMAP(records)
    mail.imap_factory = lambda *_, **__: mailbox
    assert mail.sync_account(wid, aid) == {'imported': 2, 'duplicates': 0, 'ignored': 0}
    assert mailbox.auth == ('laura@seller-business.com', 'fake-password')
    assert mail.sync_account(wid, aid) == {'imported': 0, 'duplicates': 0, 'ignored': 0}
    assert mailbox.searches == ['1:*', '3:*']
    with sessions() as db:
        assert db.get(Account, aid).imap_last_uid == 2
        rows = list(db.scalars(select(Message).where(Message.direction == 'inbound')))
        assert all('<p>' not in row.body and 'bad()' not in row.body for row in rows)
        assert len(rows) == 2
    mailbox.validity = '11'
    assert mail.sync_account(wid, aid)['duplicates'] == 2


def test_uid_fallback_keeps_distinct_messages_without_header_ids(setup):
    mail, sessions, (wid, aid, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    raw = b'From: ana@buyer-business.com\r\nSubject: Re\r\n\r\nHello'
    mailbox = FakeIMAP({3: raw, 4: raw})
    mail.imap_factory = lambda *_, **__: mailbox
    assert mail.sync_account(wid, aid)['imported'] == 2
    with sessions() as db:
        ids = list(db.scalars(select(Message.provider_id).where(Message.direction == 'inbound')))
        assert ids == ['imap:10:3', 'imap:10:4']


def test_imap_reports_backlog_and_only_clears_error_after_complete_empty_sync(setup):
    mail, sessions, (wid, aid, cid, _) = setup
    mail.prepare_campaign(wid, cid)
    raw = b'From: unknown@other-business.com\r\nSubject: Unrelated\r\n\r\nHello'
    mailbox = FakeIMAP({uid: raw for uid in range(1, 202)})
    mail.imap_factory = lambda *_, **__: mailbox
    first = mail.sync_account(wid, aid)
    assert first['has_more'] and first['ignored'] == 200
    with sessions() as db:
        account = db.get(Account, aid)
        assert account.imap_last_uid == 200 and 'backlog' in account.last_error
    assert mail.sync_account(wid, aid) == {'imported': 0, 'duplicates': 0, 'ignored': 1}
    with sessions() as db:
        account = db.get(Account, aid)
        assert account.last_error == ''
        account.last_error = 'IMAP: Previous failure'
        db.commit()
    assert mail.sync_account(wid, aid) == {'imported': 0, 'duplicates': 0, 'ignored': 0}
    with sessions() as db:
        account = db.get(Account, aid)
        assert account.last_error == ''
        account.last_error = 'SMTPAuthenticationError'
        db.commit()
    mail.sync_account(wid, aid)
    with sessions() as db:
        assert db.get(Account, aid).last_error == 'SMTPAuthenticationError'
