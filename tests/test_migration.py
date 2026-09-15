import hashlib
import sqlite3

import pytest
from sqlalchemy import func, select

from egasis.migrate_legacy import MigrationError, import_legacy, main
from egasis.models import Account, Campaign, Connection, Contact, Job, Meeting, Message, Suppression, Workspace
from egasis.store import make_store


@pytest.fixture
def migration(tmp_path):
    source = tmp_path / 'previous.db'
    with sqlite3.connect(source) as db:
        db.executescript('''
            CREATE TABLE leads(id INTEGER PRIMARY KEY, email TEXT, name TEXT, company TEXT, status TEXT);
            CREATE TABLE sent_emails(id INTEGER PRIMARY KEY, lead_id INTEGER, subject TEXT, body TEXT, unsubscribed INTEGER);
            CREATE TABLE replies(id INTEGER PRIMARY KEY, lead_id INTEGER, from_email TEXT, subject TEXT, body TEXT);
            CREATE TABLE email_accounts(id INTEGER, app_password TEXT);
            CREATE TABLE api_keys(id INTEGER, api_key TEXT);
            CREATE TABLE meetings(id INTEGER, lead_id INTEGER, meeting_date TEXT);
            INSERT INTO leads VALUES(1, 'ALICE@example.com', 'Alice', 'Company', 'sent');
            INSERT INTO leads VALUES(2, 'alice@example.com', 'Alice', 'Company', 'sent');
            INSERT INTO leads VALUES(3, 'stop@example.com', 'Stop', 'Other', 'do_not_contact');
            INSERT INTO leads VALUES(4, 'invalid', 'Invalid', '', 'pending');
            INSERT INTO sent_emails VALUES(1, 1, 'Same subject', 'Historical outbound', 0);
            INSERT INTO sent_emails VALUES(2, 3, 'Another', 'Unsubscribed', 1);
            INSERT INTO replies VALUES(1, 1, 'alice@example.com', 'Same subject', 'First reply');
            INSERT INTO replies VALUES(2, 1, 'alice@example.com', 'Same subject', 'Second reply');
            INSERT INTO email_accounts VALUES(1, 'SECRET_PASSWORD');
            INSERT INTO api_keys VALUES(1, 'SECRET_API_KEY');
            INSERT INTO meetings VALUES(1, 1, '2026-10-01');
        ''')
    engine, factory = make_store('sqlite:///' + str(tmp_path / 'new.db'))
    with factory.begin() as db:
        db.add_all([Workspace(name='Destination'), Workspace(name='Other tenant')])
    yield source, factory, engine
    engine.dispose()


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_dry_run_default_preserves_source_and_destination(migration):
    source, factory, _ = migration
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    report = import_legacy(source, factory, 1)
    assert report['dry_run'] is True
    assert (report['campaigns'], report['contacts'], report['messages']) == (1, 2, 4)
    assert report['invalid_contacts'] == 1
    with factory() as db:
        assert count(db, Campaign) == count(db, Contact) == count(db, Message) == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_apply_preserves_history_and_blocks_automatic_recontact(migration):
    source, factory, _ = migration
    before = source.read_bytes()
    result = import_legacy(source, factory, 1, apply=True)
    assert result['contacts'] == 2
    assert result['suppressions'] == 1
    assert result['credentials_imported'] == result['meetings_imported'] == 0
    with factory() as db:
        assert {c.status for c in db.scalars(select(Campaign))} == {'draft'}
        assert {c.status for c in db.scalars(select(Contact))} == {'imported'}
        assert {m.status for m in db.scalars(select(Message))} == {'imported'}
        assert count(db, Job) == count(db, Account) == count(db, Connection) == count(db, Meeting) == 0
        assert count(db, Message) == 4
        assert db.scalar(select(Suppression.email)) == 'stop@example.com'
        assert not db.scalar(select(Contact).where(Contact.workspace_id == 2))
        assert all(m.sent_at is None for m in db.scalars(select(Message)))
    assert source.read_bytes() == before


def test_repeated_apply_is_deduplicated_by_source_row_and_normalized_email(migration):
    source, factory, _ = migration
    import_legacy(source, factory, 1, apply=True)
    result = import_legacy(source, factory, 1, apply=True)
    assert result['campaigns'] == result['contacts'] == result['messages'] == result['suppressions'] == 0
    assert result['duplicates'] == 7
    with factory() as db:
        assert count(db, Contact) == 2
        assert count(db, Message) == 4


def test_missing_workspace_rejected_without_import(migration):
    source, factory, _ = migration
    with pytest.raises(MigrationError):
        import_legacy(source, factory, 404, apply=True)
    with factory() as db:
        assert count(db, Contact) == 0


def test_same_source_target_refused_before_writing(migration):
    _, factory, engine = migration
    with pytest.raises(MigrationError, match='diferentes'):
        import_legacy(engine.url.database, factory, 1, apply=True)


def test_cli_requires_explicit_target_and_existing_database(migration, tmp_path, capsys):
    source, _, engine = migration
    assert main(['--source', str(source), '--target-workspace', '1', '--database-url', str(engine.url)]) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    nonexistent = tmp_path / 'missing.db'
    with pytest.raises(SystemExit) as exc:
        main(['--source', str(source), '--target-workspace', '1', '--database-url', 'sqlite:///' + str(nonexistent)])
    assert exc.value.code == 2
    assert not nonexistent.exists()


def test_nexus_client_separation_requires_selector(migration):
    source, factory, _ = migration
    with sqlite3.connect(source) as db:
        db.executescript('''
            CREATE TABLE campaigns(id INTEGER PRIMARY KEY, client_id INTEGER, name TEXT);
            INSERT INTO campaigns VALUES(10, 1, 'One');
            INSERT INTO campaigns VALUES(20, 2, 'Two');
            ALTER TABLE leads ADD COLUMN campaign_id INTEGER;
            UPDATE leads SET campaign_id=10 WHERE id=1;
            UPDATE leads SET campaign_id=20 WHERE id<>1;
        ''')
    with pytest.raises(MigrationError, match='varios clientes'):
        import_legacy(source, factory, 1, apply=True)
    result = import_legacy(source, factory, 1, apply=True, source_client=1)
    assert result['contacts'] == 1
    with factory() as db:
        assert db.scalar(select(Contact.email)) == 'alice@example.com'
        assert count(db, Campaign) == 1
