"""Integrated worker safety tests: temporary stores, mock Gemini, no live services."""
import imaplib
import json
import smtplib

import httpx
import pytest
from sqlalchemy import select

from egasis.intelligence import DEFAULT_MODEL, Intelligence
from egasis.models import Account, Campaign, Contact, Job, Meeting, Message, Suppression, Usage, Workspace
from egasis.security import Vault
from egasis.settings import Settings
from egasis.store import make_store
from egasis.worker import tick


APPROVED_REPLY = 'Hola {{name}}. Podemos contarte sobre {{offer}}.\n{{signature}}'


@pytest.fixture(autouse=True)
def no_live_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Worker tests must never contact a live service')
    monkeypatch.setattr(smtplib, 'SMTP', forbidden)
    monkeypatch.setattr(smtplib, 'SMTP_SSL', forbidden)
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)


@pytest.fixture
def setup(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'worker.sqlite')
    engine, factory = make_store(url)
    vault = Vault('worker-test-secret-only')
    settings = Settings(database_url=url, secret_key='worker-test-secret-only',
                        simulation=True, public_url='https://egasis.example.com')
    records = []
    with factory() as db:
        for suffix in ('one', 'two'):
            workspace = Workspace(name='Espacio ' + suffix, offer='Asesoría de diseño', audience='Comercios',
                                  signature='Clara · Estudio ' + suffix, timezone='UTC', daily_budget=1)
            db.add(workspace); db.flush()
            account = Account(workspace_id=workspace.id, email=f'sender-{suffix}@example.com',
                              secret=vault.encrypt('simulated-password'), cooldown_seconds=0, daily_limit=100)
            campaign = Campaign(workspace_id=workspace.id, name='Campaña ' + suffix,
                                subject='Presentación', body='Oferta configurada por el propietario',
                                status='active', reply_mode='automatic', auto_reply_body=APPROVED_REPLY,
                                weekdays='0,1,2,3,4,5,6', start_hour=0, end_hour=24, daily_limit=100)
            db.add_all([account, campaign]); db.flush()
            contact = Contact(workspace_id=workspace.id, campaign_id=campaign.id, account_id=account.id,
                              email=f'recipient-{suffix}@example.com', name='Ana', company='Comercio', status='replied')
            db.add(contact); db.flush()
            inbound = Message(workspace_id=workspace.id, contact_id=contact.id, account_id=account.id,
                              direction='inbound', status='received', subject='Consulta', body='Me interesa conocer más.',
                              provider_id=f'<inbound-{suffix}@example.com>')
            db.add(inbound); db.flush()
            records.append({'wid': workspace.id, 'aid': account.id, 'campaign': campaign.id, 'cid': contact.id,
                            'mid': inbound.id, 'signature': workspace.signature, 'offer': workspace.offer})
        db.commit()
    yield factory, vault, settings, records
    engine.dispose()


def gemini_response(classification='interested', body='Propuesta generada que no debe enviarse sola.'):
    payload = {'classification': classification, 'body': body, 'reason': 'Clasificación simulada.'}
    return httpx.Response(200, json={
        'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps(payload)}]}}],
        'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 35, 'thoughtsTokenCount': 0},
        'modelVersion': DEFAULT_MODEL,
    })


def install_ai(monkeypatch, factory, vault, handler=None, api_key='mock-api-key'):
    calls = []
    def respond(request):
        calls.append(request)
        return handler(request) if handler else gemini_response()
    ai = Intelligence(factory, vault, api_key=api_key, model=DEFAULT_MODEL,
                      input_price=0.1, output_price=0.4, transport=httpx.MockTransport(respond))
    monkeypatch.setattr('egasis.intelligence.Intelligence', lambda *args, **kwargs: ai)
    return calls


@pytest.mark.parametrize('classification', ['interested', 'question'])
def test_automatic_reply_uses_approved_template_and_is_processed_once(setup, monkeypatch, classification):
    factory, vault, settings, (one, _) = setup
    calls = install_ai(monkeypatch, factory, vault, lambda request: gemini_response(classification))
    result = tick(factory, vault, settings, workspace_id=one['wid'])
    assert result['result']['status'] == 'simulated'
    with factory() as db:
        outgoing = db.scalar(select(Message).where(Message.workspace_id == one['wid'], Message.direction == 'outbound'))
        assert outgoing.body == f"Hola Ana. Podemos contarte sobre {one['offer']}.\n{one['signature']}"
        assert outgoing.status == 'simulated' and outgoing.sent_at is None
        assert outgoing.idempotency_key == f"reply:{one['mid']}"
        assert outgoing.in_reply_to == '<inbound-one@example.com>'
        assert db.get(Message, one['mid']).status == 'processed'
        assert db.query(Job).filter_by(workspace_id=one['wid']).count() == 1
        assert db.query(Usage).filter_by(workspace_id=one['wid']).count() == 1
    tick(factory, vault, settings, workspace_id=one['wid'])
    assert len(calls) == 1
    with factory() as db:
        assert db.query(Job).filter_by(workspace_id=one['wid']).count() == 1


def test_review_mode_does_not_generate_or_queue_automatically(setup, monkeypatch):
    factory, vault, settings, (one, _) = setup
    calls = install_ai(monkeypatch, factory, vault)
    with factory() as db:
        db.get(Campaign, one['campaign']).reply_mode = 'review'; db.commit()
    assert tick(factory, vault, settings, workspace_id=one['wid'])['result']['status'] == 'idle'
    assert not calls
    with factory() as db:
        assert db.get(Message, one['mid']).status == 'received'
        assert db.query(Job).count() == 0


def test_missing_approved_template_never_generates_or_sends(setup, monkeypatch):
    factory, vault, settings, (one, _) = setup
    calls = install_ai(monkeypatch, factory, vault)
    with factory() as db:
        db.get(Campaign, one['campaign']).auto_reply_body = ''; db.commit()
    tick(factory, vault, settings, workspace_id=one['wid'])
    tick(factory, vault, settings, workspace_id=one['wid'])
    assert not calls
    with factory() as db:
        assert db.query(Job).count() == 0


@pytest.mark.parametrize('classification', [
    'unsubscribe', 'not_interested', 'out_of_office', 'bounce', 'unknown', 'objection',
])
def test_unsafe_or_review_required_classification_never_sends(setup, monkeypatch, classification):
    factory, vault, settings, (one, _) = setup
    calls = install_ai(monkeypatch, factory, vault, lambda request: gemini_response(
        classification, 'Necesitamos revisarlo.' if classification == 'objection' else ''))
    tick(factory, vault, settings, workspace_id=one['wid'])
    with factory() as db:
        assert db.query(Job).filter_by(workspace_id=one['wid']).count() == 0
        assert db.get(Message, one['mid']).status == 'needs_review'
        assert db.query(Message).filter(Message.workspace_id == one['wid'], Message.status.in_(['queued', 'simulated', 'sent'])).count() == 0
        if classification in {'unsubscribe', 'not_interested', 'bounce'}:
            assert db.query(Suppression).filter_by(workspace_id=one['wid']).count() == 1
    tick(factory, vault, settings, workspace_id=one['wid'])
    assert len(calls) == 1


def test_existing_unknown_draft_cannot_bypass_worker_classification_gate(setup, monkeypatch):
    factory, vault, settings, (one, _) = setup
    calls = install_ai(monkeypatch, factory, vault)
    with factory() as db:
        db.get(Message, one['mid']).classification = 'interested'
        db.add(Message(workspace_id=one['wid'], contact_id=one['cid'], account_id=one['aid'],
                       direction='outbound', status='draft', subject='Re: Consulta', body='No validado',
                       classification='unknown', idempotency_key=f"reply:{one['mid']}", in_reply_to='<inbound-one@example.com>'))
        db.commit()
    tick(factory, vault, settings, workspace_id=one['wid'])
    assert not calls
    with factory() as db:
        assert db.query(Job).count() == 0
        assert db.get(Message, one['mid']).status == 'needs_review'


def test_existing_human_reply_is_processed_without_ai_rewrite_or_duplicate_job(setup, monkeypatch):
    factory, vault, settings, (one, _) = setup
    calls = install_ai(monkeypatch, factory, vault)
    with factory() as db:
        replied = Message(workspace_id=one['wid'], contact_id=one['cid'], account_id=one['aid'],
                          direction='outbound', status='queued', subject='Re: Consulta', body='Respuesta escrita por Clara.',
                          classification='unclassified', idempotency_key=f"reply:{one['mid']}", in_reply_to='<inbound-one@example.com>')
        db.add(replied); db.flush()
        db.add(Job(workspace_id=one['wid'], message_id=replied.id, due_at=0))
        db.commit(); reply_id = replied.id
    tick(factory, vault, settings, workspace_id=one['wid'])
    tick(factory, vault, settings, workspace_id=one['wid'])
    assert not calls
    with factory() as db:
        assert db.get(Message, one['mid']).status == 'processed'
        assert db.get(Message, reply_id).body == 'Respuesta escrita por Clara.'
        assert db.get(Message, reply_id).status == 'simulated'
        assert db.query(Job).filter_by(workspace_id=one['wid']).count() == 1


def test_suppressed_contact_is_not_sent_or_charged(setup, monkeypatch):
    factory, vault, settings, (one, _) = setup
    calls = install_ai(monkeypatch, factory, vault)
    with factory() as db:
        db.add(Suppression(workspace_id=one['wid'], email='recipient-one@example.com', reason='unsubscribe'))
        db.commit()
    tick(factory, vault, settings, workspace_id=one['wid'])
    assert not calls
    with factory() as db:
        assert db.query(Usage).count() == 0
        assert db.query(Job).count() == 0


def test_suppression_during_generation_prevents_automatic_send(setup, monkeypatch):
    factory, vault, settings, (one, _) = setup
    def response(request):
        with factory() as db:
            db.add(Suppression(workspace_id=one['wid'], email='recipient-one@example.com', reason='unsubscribe'))
            db.commit()
        return gemini_response()
    calls = install_ai(monkeypatch, factory, vault, response)
    tick(factory, vault, settings, workspace_id=one['wid'])
    assert len(calls) == 1
    with factory() as db:
        assert db.query(Job).count() == 0
        assert db.get(Message, one['mid']).status == 'needs_review'


def test_provider_failure_is_visible_and_never_repeatedly_spends(setup, monkeypatch):
    factory, vault, settings, (one, _) = setup
    calls = install_ai(monkeypatch, factory, vault, lambda request: httpx.Response(503))
    tick(factory, vault, settings, workspace_id=one['wid'])
    tick(factory, vault, settings, workspace_id=one['wid'])
    assert len(calls) == 1
    with factory() as db:
        assert db.get(Message, one['mid']).status == 'needs_review'
        assert db.query(Job).count() == 0
        usage = db.scalar(select(Usage).where(Usage.workspace_id == one['wid']))
        assert usage.operation.startswith('uncertain:') and usage.cost > 0


def test_generated_price_and_confirmed_booking_never_reach_automatic_message(setup, monkeypatch):
    factory, vault, settings, (one, _) = setup
    install_ai(monkeypatch, factory, vault, lambda request: gemini_response('interested',
        'El precio final es USD 999 y la reunión quedó confirmada para mañana a las 10:00.'))
    tick(factory, vault, settings, workspace_id=one['wid'])
    with factory() as db:
        outgoing = db.scalar(select(Message).where(Message.workspace_id == one['wid'], Message.direction == 'outbound'))
        assert outgoing.status == 'simulated'
        assert '999' not in outgoing.body and 'confirmada' not in outgoing.body and '10:00' not in outgoing.body
        assert outgoing.body == f"Hola Ana. Podemos contarte sobre {one['offer']}.\n{one['signature']}"
        assert db.query(Meeting).count() == 0


def test_workspace_simulation_tick_leaves_other_campaign_inbound_and_job_untouched(setup, monkeypatch):
    factory, vault, settings, (one, two) = setup
    calls = install_ai(monkeypatch, factory, vault)
    with factory() as db:
        db.get(Contact, two['cid']).status = 'pending'
        queued = Message(workspace_id=two['wid'], contact_id=two['cid'], account_id=two['aid'], direction='outbound',
                         status='queued', subject='Otro espacio', body='No debe procesarse', idempotency_key='other-space-queue')
        db.add(queued); db.flush()
        job = Job(workspace_id=two['wid'], message_id=queued.id, due_at=0)
        db.add(job); db.commit()
        queued_id, job_id = queued.id, job.id
    tick(factory, vault, settings, workspace_id=one['wid'])
    assert len(calls) == 1
    with factory() as db:
        assert db.get(Message, two['mid']).status == 'received'
        assert db.get(Contact, two['cid']).status == 'pending'
        assert db.get(Message, queued_id).status == 'queued'
        assert db.get(Job, job_id).status == 'pending'
        assert db.query(Job).filter_by(workspace_id=two['wid']).count() == 1
        assert db.query(Usage).filter_by(workspace_id=two['wid']).count() == 0


def test_invalid_template_variable_never_reaches_delivery(setup, monkeypatch):
    factory, vault, settings, (one, _) = setup
    install_ai(monkeypatch, factory, vault)
    with factory() as db:
        db.get(Campaign, one['campaign']).auto_reply_body = 'Hola {{secret}}'; db.commit()
    tick(factory, vault, settings, workspace_id=one['wid'])
    with factory() as db:
        assert db.query(Message).filter(Message.workspace_id == one['wid'], Message.status.in_(['queued', 'simulated', 'sent'])).count() == 0


def test_automatic_approved_booking_link_is_signed_without_creating_reservation(setup, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    factory, vault, settings, (one, _) = setup
    install_ai(monkeypatch, factory, vault)
    with factory() as db:
        db.get(Campaign, one['campaign']).auto_reply_body = 'Elegí un horario: {{booking_link}}'
        db.commit()
    tick(factory, vault, settings, workspace_id=one['wid'])
    with factory() as db:
        outgoing = db.scalar(select(Message).where(Message.workspace_id == one['wid'], Message.direction == 'outbound'))
        link = outgoing.body.split('Elegí un horario: ')[1]
        query = parse_qs(urlsplit(link).query)
        assert vault.verify_booking(one['wid'], one['cid'], query['token'][0])
        assert outgoing.status == 'simulated'
        assert db.query(Meeting).count() == 0


def test_worker_blocks_real_reply_until_imap_is_successful(setup, monkeypatch):
    from egasis.mail import MailEngine
    factory, vault, settings, (one, _) = setup
    settings.simulation = False
    install_ai(monkeypatch, factory, vault)
    synced = []
    def fail_sync(self, wid, aid):
        synced.append((wid, aid))
        raise OSError('IMAP unavailable')
    monkeypatch.setattr(MailEngine, 'sync_account', fail_sync)
    monkeypatch.setattr(MailEngine, '_deliver', lambda *_: pytest.fail('Worker sent while IMAP failed'))
    result = tick(factory, vault, settings, workspace_id=one['wid'])
    assert synced == [(one['wid'], one['aid'])]
    assert result['result']['status'] == 'blocked'
    with factory() as db:
        job = db.scalar(select(Job).where(Job.workspace_id == one['wid']))
        assert job.status == 'pending' and job.attempts == 0
