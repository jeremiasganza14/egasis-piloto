import json
import time
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from sqlalchemy import select

from egasis.calendar_service import CalendarError, CalendarService, GOOGLE_API, GOOGLE_TOKEN
from egasis.models import Campaign, Connection, Contact, Event, Meeting, Workspace
from egasis.security import Vault
from egasis.store import make_store


class GoogleStub:
    def __init__(self):
        self.events = {}
        self.requests = []
        self.busy_error = False
        self.force_busy = []
        self.api_status = None
        self.post_timeout = False
        self.post_conflict = False
        self.delete_status = 204
        self.expired = False
        self.token_status = 200
        self.created_status = 'confirmed'

    def __call__(self, request):
        self.requests.append(request)
        if str(request.url) == GOOGLE_TOKEN:
            return httpx.Response(self.token_status, json={'access_token': 'fresh-access', 'expires_in': 3600})
        if self.api_status:
            return httpx.Response(self.api_status, json={'error': 'DO_NOT_EXPOSE_SECRET'})
        if self.expired and request.headers.get('authorization') != 'Bearer fresh-access':
            return httpx.Response(401, json={})
        path = unquote(request.url.path)
        if path.endswith('/freeBusy'):
            body = json.loads(request.content)
            calendar_id = body['items'][0]['id']
            if self.busy_error:
                return httpx.Response(200, json={'calendars': {calendar_id: {'errors': [{'reason': 'notFound'}]}}})
            return httpx.Response(200, json={'calendars': {calendar_id: {'busy': self.force_busy}}})
        parts = path.split('/calendars/', 1)[1].split('/events')
        calendar_id, suffix = parts[0], parts[1].lstrip('/')
        if request.method == 'GET':
            event = self.events.get((calendar_id, suffix))
            return httpx.Response(200 if event else 404, json=event or {})
        if request.method == 'POST':
            body = json.loads(request.content)
            event = {**body, 'status': self.created_status, 'htmlLink': 'https://calendar.google.com/event'}
            key = (calendar_id, body['id'])
            conflict = key in self.events or self.post_conflict
            self.events[key] = event
            if self.post_timeout:
                self.post_timeout = False
                raise httpx.ReadTimeout('uncertain response', request=request)
            return httpx.Response(409 if conflict else 201, json={} if conflict else event)
        if request.method == 'DELETE':
            if self.delete_status == 204:
                self.events.pop((calendar_id, suffix), None)
            return httpx.Response(self.delete_status)
        raise AssertionError('Unexpected mocked request')


@pytest.fixture
def calendar(tmp_path):
    engine, factory = make_store('sqlite:///' + str(tmp_path / 'calendar.db'))
    vault = Vault('test-local-calendar-key')
    credentials = {'access_token': 'private-access', 'refresh_token': 'private-refresh',
                   'client_id': 'private-client', 'client_secret': 'private-secret',
                   'calendar_id': 'owner@example.com'}
    with factory.begin() as db:
        one, two = Workspace(name='One'), Workspace(name='Two')
        db.add_all([one, two]); db.flush()
        campaign = Campaign(workspace_id=one.id, name='Campaign', subject='Subject', body='Body')
        db.add(campaign); db.flush()
        contact = Contact(workspace_id=one.id, campaign_id=campaign.id, email='prospect@example.com', name='Prospect')
        db.add(contact); db.flush()
        meeting = Meeting(workspace_id=one.id, contact_id=contact.id, starts_at=int(time.time()) + 3600,
                          duration_minutes=30, notes='Explicit owner reservation', location='Office')
        db.add(meeting)
        db.add(Connection(workspace_id=one.id, provider='google_calendar', secret=vault.encrypt(json.dumps(credentials))))
    stub = GoogleStub()
    with httpx.Client(transport=httpx.MockTransport(stub)) as client:
        yield CalendarService(factory, vault, client), factory, vault, stub
    engine.dispose()


def meeting(factory):
    with factory() as db:
        return db.get(Meeting, 1)


def test_availability_returns_verified_busy_intervals(calendar):
    service, factory, _, stub = calendar
    row = meeting(factory)
    stub.force_busy = [{'start': '2026-10-01T09:00:00-03:00', 'end': '2026-10-01T09:30:00-03:00'}]
    result = service.availability(1, row.starts_at, row.starts_at + 3600)
    assert result['calendar_id'] == 'owner@example.com'
    assert result['busy'] == [{'start': '2026-10-01T12:00:00Z', 'end': '2026-10-01T12:30:00Z'}]
    assert all(str(request.url).startswith(GOOGLE_API) for request in stub.requests)


def test_freebusy_provider_error_never_looks_like_a_free_slot(calendar):
    service, factory, _, stub = calendar
    stub.busy_error = True
    row = meeting(factory)
    with pytest.raises(CalendarError, match='disponibilidad'):
        service.availability(1, row.starts_at, row.starts_at + 3600)


@pytest.mark.parametrize('start,end', [('2026-10-01', '2026-10-02'), (100, 99), (float('nan'), 200)])
def test_invalid_time_range_does_not_contact_google(calendar, start, end):
    service, _, _, stub = calendar
    with pytest.raises(CalendarError):
        service.availability(1, start, end)
    assert not stub.requests


def test_create_confirms_only_provider_result_without_sending_guest_invites(calendar):
    service, factory, _, stub = calendar
    result = service.create_event(1, 1)
    assert result == {'meeting_id': 1, 'external_id': 'egasis1meeting1', 'status': 'confirmed', 'calendar_id': 'owner@example.com'}
    assert meeting(factory).status == 'confirmed'
    inserts = [r for r in stub.requests if r.method == 'POST' and r.url.path.endswith('/events')]
    assert len(inserts) == 1
    payload = json.loads(inserts[0].content)
    assert payload['id'] == 'egasis1meeting1'
    assert 'attendees' not in payload
    assert inserts[0].url.params['sendUpdates'] == 'none'
    assert service.create_event(1, 1) == result
    assert len([r for r in stub.requests if r.method == 'POST' and r.url.path.endswith('/events')]) == 1


def test_insert_conflict_reconciles_matching_remote_event(calendar):
    service, factory, _, stub = calendar
    stub.post_conflict = True
    assert service.create_event(1, 1)['status'] == 'confirmed'
    assert len([r for r in stub.requests if r.method == 'GET']) == 2
    assert meeting(factory).external_id == 'egasis1meeting1'


def test_network_uncertainty_reuses_event_and_original_calendar_after_connection_change(calendar):
    service, factory, vault, stub = calendar
    stub.post_timeout = True
    with pytest.raises(CalendarError) as error:
        service.create_event(1, 1)
    assert error.value.status_code == 503
    assert meeting(factory).external_id is None
    assert meeting(factory).status == 'proposed'
    with factory.begin() as db:
        connection = db.scalar(select(Connection))
        credentials = json.loads(vault.decrypt(connection.secret))
        credentials['calendar_id'] = 'another@example.com'
        connection.secret = vault.encrypt(json.dumps(credentials))
        assert db.scalar(select(Event).where(Event.kind == 'calendar.intent'))
    assert service.create_event(1, 1)['calendar_id'] == 'owner@example.com'
    assert len(stub.events) == 1
    assert len([r for r in stub.requests if r.method == 'POST' and r.url.path.endswith('/events')]) == 1


def test_cross_workspace_cannot_create_or_cancel_event(calendar):
    service, _, _, stub = calendar
    for action in (service.create_event, service.cancel_event):
        with pytest.raises(CalendarError) as error:
            action(2, 1)
        assert error.value.status_code == 404
    assert not stub.requests


def test_remote_failure_and_tentative_state_do_not_claim_confirmation(calendar):
    service, factory, _, stub = calendar
    stub.api_status = 403
    with pytest.raises(CalendarError) as error:
        service.create_event(1, 1)
    assert 'DO_NOT_EXPOSE_SECRET' not in str(error.value)
    assert meeting(factory).status == 'proposed'
    assert meeting(factory).external_id is None
    stub.api_status = None
    stub.created_status = 'tentative'
    with pytest.raises(CalendarError, match='todavía no confirmó'):
        service.create_event(1, 1)
    assert meeting(factory).status == 'proposed'


def test_busy_slot_does_not_create_event(calendar):
    service, factory, _, stub = calendar
    stub.force_busy = [{'start': '2026-10-01T12:00:00Z', 'end': '2026-10-01T13:00:00Z'}]
    with pytest.raises(CalendarError, match='ocupado'):
        service.create_event(1, 1)
    assert not stub.events
    assert meeting(factory).external_id is None


def test_foreign_event_with_same_id_is_not_adopted_or_deleted(calendar):
    service, factory, _, stub = calendar
    stub.events[('owner@example.com', 'egasis1meeting1')] = {'id': 'egasis1meeting1', 'status': 'confirmed'}
    with pytest.raises(CalendarError, match='no coincide'):
        service.create_event(1, 1)
    with pytest.raises(CalendarError, match='no coincide'):
        service.cancel_event(1, 1)
    assert meeting(factory).external_id is None
    assert not [r for r in stub.requests if r.method == 'DELETE']


def test_changed_remote_time_requires_review(calendar):
    service, factory, _, stub = calendar
    service.create_event(1, 1)
    original = meeting(factory).starts_at
    stub.events[('owner@example.com', 'egasis1meeting1')]['start']['dateTime'] = '2030-01-01T00:00:00Z'
    with pytest.raises(CalendarError, match='horario'):
        service.create_event(1, 1)
    assert meeting(factory).starts_at == original


def test_cancel_waits_for_provider_success_and_can_retry(calendar):
    service, factory, _, stub = calendar
    service.create_event(1, 1)
    stub.delete_status = 503
    with pytest.raises(CalendarError):
        service.cancel_event(1, 1)
    assert meeting(factory).status == 'confirmed'
    stub.delete_status = 204
    result = service.cancel_event(1, 1)
    assert result['status'] == 'cancelled'
    assert result['external_id'] == 'egasis1meeting1'
    assert not stub.events
    requests = len(stub.requests)
    assert service.cancel_event(1, 1) == result
    assert len(stub.requests) == requests
    with pytest.raises(CalendarError, match='nueva reunión'):
        service.create_event(1, 1)


def test_remote_deleted_tombstone_reconciles_known_binding(calendar):
    service, factory, _, stub = calendar
    service.create_event(1, 1)
    stub.events[('owner@example.com', 'egasis1meeting1')] = {'id': 'egasis1meeting1', 'status': 'cancelled'}
    assert service.create_event(1, 1)['status'] == 'cancelled'
    assert meeting(factory).status == 'cancelled'


def test_expired_token_refreshes_and_persists_encrypted_credentials(calendar, monkeypatch):
    service, factory, vault, stub = calendar
    with factory.begin() as db:
        connection = db.scalar(select(Connection))
        credentials = json.loads(vault.decrypt(connection.secret))
        credentials.pop('client_id'); credentials.pop('client_secret')
        connection.secret = vault.encrypt(json.dumps(credentials))
    monkeypatch.setenv('EGASIS_GOOGLE_CLIENT_ID', 'env-client')
    monkeypatch.setenv('EGASIS_GOOGLE_CLIENT_SECRET', 'env-secret')
    stub.expired = True
    row = meeting(factory)
    assert service.availability(1, row.starts_at, row.starts_at + 3600)['busy'] == []
    requests = [r for r in stub.requests if str(r.url) == GOOGLE_TOKEN]
    assert len(requests) == 1
    assert parse_qs(requests[0].content.decode()) == {'client_id': ['env-client'], 'client_secret': ['env-secret'],
                                                    'refresh_token': ['private-refresh'], 'grant_type': ['refresh_token']}
    with factory() as db:
        encoded = db.scalar(select(Connection.secret))
        assert 'fresh-access' not in encoded
        assert json.loads(vault.decrypt(encoded))['access_token'] == 'fresh-access'


def test_rejected_refresh_does_not_expose_tokens_or_confirm(calendar):
    service, factory, _, stub = calendar
    stub.expired, stub.token_status = True, 400
    with pytest.raises(CalendarError) as error:
        service.create_event(1, 1)
    assert error.value.status_code == 401
    assert 'private-' not in str(error.value)
    assert meeting(factory).status == 'proposed'
    assert meeting(factory).external_id is None
