"""Optional integration tests against an explicitly selected temporary PostgreSQL.

EGASIS_TEST_POSTGRES_URL must name an egasis_test_* database on a numeric loopback
host and an explicit non-default port. Each test creates a unique child database,
and cleanup drops only those children. No production/default cluster is selected.
"""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import smtplib
import imaplib
import subprocess
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from egasis import schema
from egasis.app import create_app
from egasis.intelligence import BudgetExceeded, DEFAULT_MODEL, Intelligence
from egasis.mail import MailEngine
from egasis.models import Account, Base, Campaign, Contact, Job, Message, Usage, Workspace
from egasis.security import Vault
from egasis.settings import Settings
from egasis.store import make_store


PG_URL = os.getenv('EGASIS_TEST_POSTGRES_URL', '')
pytestmark = pytest.mark.skipif(not PG_URL, reason='Set EGASIS_TEST_POSTGRES_URL to an isolated local test database')


def guarded_url(raw):
    value = make_url(raw)
    try:
        loopback = ipaddress.ip_address(value.host or '').is_loopback
    except ValueError:
        loopback = False
    if (value.drivername not in {'postgresql', 'postgresql+psycopg2'} or not loopback
            or not value.port or value.port < 1024 or value.port == 5432 or value.query
            or not re.fullmatch(r'egasis_test_[a-z0-9_]{1,32}', value.database or '')):
        raise ValueError('PostgreSQL QA requires a numeric loopback host, explicit non-default port and egasis_test_* database without URL query overrides')
    return value


@pytest.fixture(autouse=True)
def no_live_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('No email or external HTTP calls are allowed in PostgreSQL QA')
    monkeypatch.setattr(smtplib, 'SMTP', forbidden)
    monkeypatch.setattr(smtplib, 'SMTP_SSL', forbidden)
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)


@pytest.fixture
def make_database():
    base = guarded_url(PG_URL)
    admin = create_engine(base, isolation_level='AUTOCOMMIT', connect_args={'connect_timeout': 5})
    children = []
    def create():
        name = 'egasis_test_' + uuid4().hex[:20]
        assert re.fullmatch(r'egasis_test_[a-f0-9]{20}', name)
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        children.append(name)
        return base.set(database=name).render_as_string(hide_password=False)
    try:
        yield create
    finally:
        for name in reversed(children):
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture
def store(make_database):
    engine, factory = make_store(make_database())
    try:
        yield engine, factory
    finally:
        engine.dispose()


def seed(factory, *, contacts=1):
    vault = Vault('postgres-qa-synthetic-key')
    with factory() as db:
        workspace = Workspace(name='Proveedor industrial ficticio', offer='Mantenimiento de equipos',
                              audience='Plantas industriales', timezone='UTC', daily_budget=1)
        db.add(workspace); db.flush()
        account = Account(workspace_id=workspace.id, email='sender@example.com', secret=vault.encrypt('mock-password'),
                          daily_limit=100, cooldown_seconds=0)
        campaign = Campaign(workspace_id=workspace.id, name='Campaña sintética', offer='Mantenimiento de equipos',
                            subject='Idea para {{company}}', body='Hola {{name}}. {{offer}}',
                            status='active', start_hour=0, end_hour=24, weekdays='0,1,2,3,4,5,6', daily_limit=100)
        db.add_all([account, campaign]); db.flush()
        people = [Contact(workspace_id=workspace.id, campaign_id=campaign.id, account_id=account.id,
                          email=f'contact-{index}@example.com', name='Ana', company='Fábrica ficticia') for index in range(contacts)]
        db.add_all(people); db.commit()
        return vault, workspace.id, account.id, campaign.id, [person.id for person in people]


def test_guard_rejects_unsafe_database_targets():
    unsafe = [
        'postgresql://user@db.example.com:55479/egasis_test_qa',
        'postgresql://user@127.0.0.1:5432/egasis_test_qa',
        'postgresql://user@127.0.0.1:55479/production',
        'postgresql://user@127.0.0.1:55479/egasis_test_qa?host=remote.example.com',
        'sqlite:///egasis_test_qa',
    ]
    for url in unsafe:
        with pytest.raises(ValueError):
            guarded_url(url)


def test_real_empty_database_bootstraps_and_current_schema_matches_orm(store):
    engine, factory = store
    assert schema.schema_status(engine)['version'] == schema.CURRENT_VERSION
    assert set(inspect(engine).get_table_names()) == set(Base.metadata.tables) | {schema.MANIFEST_TABLE}
    for name, table in Base.metadata.tables.items():
        assert {column['name'] for column in inspect(engine).get_columns(name)} == set(table.c.keys())
    with engine.connect() as connection:
        assert connection.execute(text('SELECT version FROM _egasis_schema_migrations ORDER BY version')).scalars().all() == list(range(1, schema.CURRENT_VERSION + 1))
        assert connection.execute(text('SHOW server_version')).scalar().split('.')[0] == '17'
    _, wid, aid, cid, contacts = seed(factory)
    with factory() as db:
        assert wid > 0 and aid > 0 and cid > 0 and contacts[0] > 0
        assert db.get(Account, aid).active is True


def test_v1_upgrade_to_v2_preserves_rows_and_requires_explicit_upgrade(make_database):
    engine = create_engine(make_database())
    try:
        schema.upgrade_schema(engine, target_version=1)
        with engine.begin() as connection:
            wid = connection.execute(text("INSERT INTO workspaces (name) VALUES ('Existing synthetic workspace') RETURNING id")).scalar_one()
            connection.execute(text("INSERT INTO accounts (workspace_id,email,secret) VALUES (:wid,'qa@example.com','encrypted-test')"), {'wid': wid})
            connection.execute(text("INSERT INTO campaigns (workspace_id,name,subject,body) VALUES (:wid,'Existing campaign','Subject','Body')"), {'wid': wid})
        with pytest.raises(schema.MigrationRequired):
            schema.ensure_schema(engine)
        assert schema.upgrade_schema(engine, target_version=2)['version'] == 2
        with engine.connect() as connection:
            assert connection.execute(text('SELECT name FROM workspaces')).scalar_one() == 'Existing synthetic workspace'
            assert tuple(connection.execute(text('SELECT imap_uidvalidity,imap_last_uid FROM accounts')).one()) == ('', 0)
            assert connection.execute(text('SELECT auto_reply_body FROM campaigns')).scalar_one() == ''
        assert 'reserved_at' in {column['name'] for column in inspect(engine).get_columns('jobs')}
        if schema.CURRENT_VERSION > 2:
            with pytest.raises(schema.MigrationRequired):
                schema.ensure_schema(engine)
            assert schema.upgrade_schema(engine)['version'] == schema.CURRENT_VERSION
    finally:
        engine.dispose()


def test_unversioned_v2_requires_explicit_adoption(make_database):
    engine = create_engine(make_database())
    try:
        # The only supported unversioned release predates the v3 manifest.
        schema.metadata_for_version(2).create_all(engine)
        with pytest.raises(schema.MigrationRequired):
            schema.ensure_schema(engine)
        assert schema.upgrade_schema(engine, adopt_unversioned=True)['version'] == schema.CURRENT_VERSION
    finally:
        engine.dispose()


def test_postgres_ddl_and_manifest_roll_back_together(make_database, monkeypatch):
    engine = create_engine(make_database())
    try:
        schema.upgrade_schema(engine, target_version=1)
        original = schema._validate_schema
        def fail_second(connection, version, **kwargs):
            original(connection, version, **kwargs)
            if version == 2:
                raise schema.SchemaError('Synthetic rollback trigger')
        monkeypatch.setattr(schema, '_validate_schema', fail_second)
        with pytest.raises(schema.SchemaError, match='Synthetic'):
            schema.upgrade_schema(engine, target_version=2)
        assert 'auto_reply_body' not in {column['name'] for column in inspect(engine).get_columns('campaigns')}
        with engine.connect() as connection:
            assert connection.execute(text('SELECT max(version) FROM _egasis_schema_migrations')).scalar_one() == 1
    finally:
        engine.dispose()


def test_concurrent_empty_bootstrap_is_serialized(make_database):
    url = make_database()
    start = threading.Barrier(2)
    def initialize(_):
        engine = create_engine(url)
        try:
            start.wait(timeout=5)
            return schema.ensure_schema(engine)
        finally:
            engine.dispose()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(initialize, range(2)))
    assert all(outcome['version'] == schema.CURRENT_VERSION for outcome in outcomes)


def register(client, suffix):
    response = client.post('/api/register', json={'name': 'Espacio ' + suffix,
        'email': f'owner-{suffix}@example.com', 'password': 'Synthetic-password-for-PG-tests'})
    assert response.status_code == 200, response.text


def setup_api_campaign(client):
    account = client.post('/api/accounts', json={'email': 'sender@example.com', 'password': 'test-only-password'})
    assert account.status_code == 200, account.text
    campaign = client.post('/api/campaigns', json={'name': 'Servicios industriales', 'offer': 'Mantenimiento de máquinas',
        'subject': 'Idea para {{company}}', 'body': 'Hola {{name}}. {{offer}}',
        'start_hour': 0, 'end_hour': 24, 'weekdays': '0,1,2,3,4,5,6'})
    assert campaign.status_code == 200, campaign.text
    imported = client.post(f'/api/contacts/import/{campaign.json()["id"]}', json={'contacts': [
        {'email': 'buyer@example.com', 'company': 'Empresa ficticia', 'name': 'Ana'}]})
    assert imported.status_code == 200, imported.text
    return account.json(), campaign.json(), client.get('/api/contacts').json()[0]


def test_real_postgres_api_registration_tenant_boundary_and_simulated_campaign(make_database):
    app = create_app(Settings(make_database(), 'postgres-qa-synthetic-key', simulation=True))
    try:
        with TestClient(app) as first:
            register(first, 'one')
            account, campaign, contact = setup_api_campaign(first)
            assert 'secret' not in account
            with TestClient(app) as other:
                register(other, 'two')
                assert other.get('/api/contacts').json() == []
                assert other.get('/api/campaigns').json() == []
                assert other.post(f'/api/campaigns/{campaign["id"]}/start').status_code == 404
                assert other.post(f'/api/contacts/{contact["id"]}/suppress').status_code == 404
                assert other.post('/api/engine/tick').json()['result']['status'] == 'idle'
            started = first.post(f'/api/campaigns/{campaign["id"]}/start')
            assert started.status_code == 200 and started.json()['queued'] == 1
            outcome = first.post('/api/engine/tick')
            assert outcome.status_code == 200, outcome.text
            assert outcome.json()['result']['status'] == 'simulated'
            metrics = first.get('/api/metrics').json()
            assert metrics['simulated'] == 1 and metrics['sent'] == 0
            with app.state.factory() as db:
                stored = db.get(Account, account['id'])
                assert stored.secret != 'test-only-password'
                assert app.state.vault.decrypt(stored.secret) == 'test-only-password'
    finally:
        app.state.factory.kw['bind'].dispose()


def test_concurrent_http_replies_create_exactly_one_outbox_job(make_database):
    app = create_app(Settings(make_database(), 'postgres-qa-synthetic-key', simulation=True))
    try:
        with TestClient(app) as client:
            register(client, 'reply')
            account, campaign, contact = setup_api_campaign(client)
            wid = client.get('/api/me').json()['workspace']['id']
            with app.state.factory() as db:
                db.get(Contact, contact['id']).account_id = account['id']
                inbound = Message(workspace_id=wid, contact_id=contact['id'], account_id=account['id'],
                    direction='inbound', status='received', subject='Consulta', body='¿Cómo trabajan?', provider_id='<postgres-inbound@example.com>')
                db.add(inbound); db.commit(); mid = inbound.id
            cookie = client.cookies.get('egasis_session')
            start = threading.Barrier(2)
            def reply(_):
                with TestClient(app) as caller:
                    caller.cookies.set('egasis_session', cookie)
                    start.wait(timeout=5)
                    return caller.post(f'/api/messages/{mid}/reply', json={'subject': 'Re: Consulta', 'body': 'Podemos explicarte el servicio.'})
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(reply, range(2)))
            assert [result.status_code for result in results] == [200, 200]
            assert results[0].json()['id'] == results[1].json()['id']
            with app.state.factory() as db:
                assert db.query(Job).count() == 1
                assert db.query(Message).filter_by(direction='outbound').count() == 1
    finally:
        app.state.factory.kw['bind'].dispose()


@pytest.mark.parametrize('constraint', ['account', 'campaign'])
def test_concurrent_postgres_mail_workers_reserve_quota_once(store, constraint):
    engine, factory = store
    vault, wid, aid, cid, people = seed(factory, contacts=2)
    with factory() as db:
        row = db.get(Account if constraint == 'account' else Campaign, aid if constraint == 'account' else cid)
        row.daily_limit = 1; db.commit()
    first = MailEngine(factory, vault, simulation=True)
    second = MailEngine(factory, vault, simulation=True)
    assert first.prepare_campaign(wid, cid) == 2
    entered, release = threading.Event(), threading.Event()
    build = first._build_message
    def hold(payload):
        entered.set()
        assert release.wait(5)
        return build(payload)
    first._build_message = hold
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(first.run_once, wid)
        try:
            assert entered.wait(5)
            assert second.run_once(wid)['status'] == 'idle'
        finally:
            release.set()
        assert pending.result(timeout=5)['status'] == 'simulated'
    with factory() as db:
        assert db.query(Message).filter_by(status='simulated').count() == 1
        assert db.query(Job).filter_by(status='pending').count() == 1


def test_postgres_ai_budget_reservation_blocks_concurrent_overspend(store):
    engine, factory = store
    vault, wid, aid, cid, people = seed(factory, contacts=2)
    entered, release = threading.Event(), threading.Event()
    def provider(request):
        with factory() as db:
            reservation = db.scalar(select(Usage).where(Usage.workspace_id == wid))
            db.get(Workspace, wid).daily_budget = reservation.cost * 1.5
            db.commit()
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{}'}]}}],
            'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 20}})
    ai = Intelligence(factory, vault, api_key='synthetic-key', model=DEFAULT_MODEL,
        input_price=.1, output_price=.4, transport=httpx.MockTransport(provider))
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(ai._generate, wid, people[0], 'postgres-qa', 'task', {}, {'type': 'object'})
        try:
            assert entered.wait(5)
            with pytest.raises(BudgetExceeded):
                ai._generate(wid, people[1], 'postgres-qa', 'task', {}, {'type': 'object'})
        finally:
            release.set()
        pending.result(timeout=5)
    with factory() as db:
        assert db.query(Usage).count() == 1
        assert db.scalar(select(Usage)).input_tokens == 100


def test_pg_dump_restore_preserves_data_manifest_and_generated_ids(store, make_database, tmp_path):
    engine, factory = store
    vault, wid, aid, cid, people = seed(factory)
    target = guarded_url(make_database())
    source = guarded_url(engine.url.render_as_string(hide_password=False))
    binaries = Path(os.getenv('EGASIS_TEST_POSTGRES_BIN', '/opt/homebrew/opt/postgresql@17/bin'))
    dump = str(binaries / 'pg_dump') if (binaries / 'pg_dump').is_file() else shutil.which('pg_dump')
    restore = str(binaries / 'pg_restore') if (binaries / 'pg_restore').is_file() else shutil.which('pg_restore')
    assert dump and restore, 'Install matching pg_dump/pg_restore or set EGASIS_TEST_POSTGRES_BIN'
    archive = tmp_path / 'synthetic-database.dump'
    def flags(url):
        return ['-h', url.host, '-p', str(url.port), '-U', url.username, '-d', url.database]
    environment = {'PATH': os.defpath, 'PGCONNECT_TIMEOUT': '5', 'PGPASSWORD': source.password or ''}
    dumped = subprocess.run([dump, *flags(source), '--format=custom', '--no-owner', '-f', str(archive)],
        env=environment, capture_output=True, timeout=30)
    assert dumped.returncode == 0, 'pg_dump failed against the synthetic database'
    assert archive.stat().st_size > 0
    restored = subprocess.run([restore, *flags(target), '--no-owner', '--no-privileges', '--exit-on-error', str(archive)],
        env=environment, capture_output=True, timeout=30)
    assert restored.returncode == 0, 'pg_restore failed against the synthetic database'
    restored_engine, restored_factory = make_store(target.render_as_string(hide_password=False))
    try:
        assert schema.schema_status(restored_engine)['version'] == schema.CURRENT_VERSION
        with restored_factory() as db:
            assert db.get(Workspace, wid).name == 'Proveedor industrial ficticio'
            assert db.get(Contact, people[0]).email == 'contact-0@example.com'
            assert vault.decrypt(db.get(Account, aid).secret) == 'mock-password'
            additional = Workspace(name='New row after restore')
            db.add(additional); db.commit()
            assert additional.id > wid
        for name in Base.metadata.tables:
            with engine.connect() as before, restored_engine.connect() as after:
                count_before = before.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one()
                count_after = after.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one()
                assert count_after == count_before + (1 if name == 'workspaces' else 0)
    finally:
        restored_engine.dispose()
