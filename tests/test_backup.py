"""Recovery fixtures only: never locate a runtime DB, .env, or encryption key."""
import json
import os
from pathlib import Path
import sqlite3
import stat

import pytest
from sqlalchemy import create_engine, select

from egasis import backup
from egasis.models import Account, Base, Campaign, Contact, Job, Message, Session, User, Workspace
from egasis.schema import CURRENT_VERSION
from egasis.store import make_store


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'fixture source #1.sqlite'
    engine, factory = make_store('sqlite:///' + str(path))
    with factory() as db:
        workspace = Workspace(name='Fixture business')
        db.add(workspace)
        db.flush()
        account = Account(workspace_id=workspace.id, email='fixture@business.test', secret='encrypted-fixture-placeholder', active=True)
        campaign = Campaign(workspace_id=workspace.id, name='Fixture campaign', subject='Configured', body='Configured', status='active')
        user = User(workspace_id=workspace.id, email='owner@business.test', password_hash='fixture-hash')
        db.add_all((account, campaign, user))
        db.flush()
        contact = Contact(workspace_id=workspace.id, campaign_id=campaign.id, account_id=account.id,
            email='buyer@business.test', status='pending')
        db.add(contact)
        db.flush()
        pending = Message(workspace_id=workspace.id, contact_id=contact.id, account_id=account.id,
            direction='outbound', subject='Pending', body='Pending', status='queued', idempotency_key='first:fixture')
        delivered = Message(workspace_id=workspace.id, contact_id=contact.id, account_id=account.id,
            direction='outbound', subject='Sent', body='Sent', status='sent', sent_at=100, provider_id='<accepted@fixture>')
        draft = Message(workspace_id=workspace.id, contact_id=contact.id, account_id=account.id,
            direction='outbound', subject='Draft', body='Draft', status='draft')
        incoming = Message(workspace_id=workspace.id, contact_id=contact.id, account_id=account.id,
            direction='inbound', subject='Received', body='Received', status='received', provider_id='<inbound@fixture>')
        db.add_all((pending, delivered, draft, incoming))
        db.flush()
        db.add(Job(workspace_id=workspace.id, message_id=pending.id, status='pending'))
        db.add(Session(token_hash='fixture-session-only', user_id=user.id, expires_at=9999999999))
        db.commit()
    yield path, factory
    engine.dispose()


def test_online_backup_captures_committed_wal_but_not_uncommitted_transaction(database, tmp_path):
    source, factory = database
    writer = sqlite3.connect(source)
    try:
        assert writer.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        writer.execute("INSERT INTO workspaces(name) VALUES ('Committed in WAL')")
        writer.commit()
        writer.execute("INSERT INTO workspaces(name) VALUES ('Not committed')")
        output = tmp_path / 'snapshot.sqlite'
        result = backup.create_backup(source, output)
        assert result['schema_version'] == CURRENT_VERSION
        assert Path(result['manifest']).exists()
        assert backup.verify_backup(output)['valid']
        with sqlite3.connect(output) as copied:
            names = [row[0] for row in copied.execute('SELECT name FROM workspaces')]
            assert 'Committed in WAL' in names and 'Not committed' not in names
            assert copied.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert not Path(str(output) + '-wal').exists()
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(result['manifest']).stat().st_mode) == 0o600
        assert 'encrypted-fixture-placeholder' not in json.dumps(result)
        assert 'fixture-hash' not in Path(result['manifest']).read_text()
    finally:
        writer.rollback()
        writer.close()


def test_restore_holds_replayable_work_and_never_changes_backup_or_original(database, tmp_path):
    source, factory = database
    copied = tmp_path / 'snapshot.sqlite'
    backup.create_backup(source, copied)
    snapshot_bytes = copied.read_bytes()
    restored = tmp_path / 'recovered.sqlite'
    result = backup.restore_backup(copied, restored)
    assert result['safety'] == {'campaigns_paused': 1, 'accounts_disabled': 1, 'contacts_held': 1,
        'inbound_review': 1, 'messages_held': 2, 'sessions_revoked': 1, 'jobs_created_for_review': 1}
    with sqlite3.connect(restored) as db:
        assert db.execute('SELECT status FROM campaigns').fetchone()[0] == 'paused'
        assert db.execute('SELECT active FROM accounts').fetchone()[0] == 0
        assert db.execute('SELECT status FROM contacts').fetchone()[0] == 'recovery_hold'
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM messages WHERE status='uncertain'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE status='uncertain'").fetchone()[0] == 2
        assert db.execute("SELECT sent_at FROM messages WHERE status='sent'").fetchone()[0] == 100
        assert db.execute("SELECT status FROM messages WHERE direction='inbound'").fetchone()[0] == 'needs_review'
        assert db.execute('SELECT secret FROM accounts').fetchone()[0] == 'encrypted-fixture-placeholder'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    with factory() as db:
        assert db.scalar(select(Campaign)).status == 'active'
        assert db.scalar(select(Account)).active
        assert db.scalar(select(Contact)).status == 'pending'
        assert db.scalar(select(Job)).status == 'pending'
        assert db.scalar(select(Session)) is not None
    assert copied.read_bytes() == snapshot_bytes
    assert backup.verify_backup(copied)['valid']
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600


def test_existing_destination_and_live_sidecars_are_never_overwritten(database, tmp_path):
    source, _ = database
    output = tmp_path / 'snapshot.sqlite'
    backup.create_backup(source, output)
    previous = output.read_bytes()
    with pytest.raises(backup.BackupError, match='ya existe'):
        backup.create_backup(source, output)
    with pytest.raises(backup.BackupError, match='ya existe'):
        backup.restore_backup(output, source)
    assert output.read_bytes() == previous
    other = tmp_path / 'recovery.sqlite'
    Path(str(other) + '-wal').write_bytes(b'other-writer')
    with pytest.raises(backup.BackupError, match='auxiliares'):
        backup.restore_backup(output, other)
    assert not other.exists()
    assert Path(str(other) + '-wal').read_bytes() == b'other-writer'


def test_tampered_snapshot_or_manifest_is_rejected_before_restore(database, tmp_path):
    source, _ = database
    output = tmp_path / 'snapshot.sqlite'
    backup.create_backup(source, output)
    original = output.read_bytes()
    changed = bytearray(original)
    changed[100] ^= 1
    output.write_bytes(changed)
    with pytest.raises(backup.BackupError, match='checksum'):
        backup.restore_backup(output, tmp_path / 'bad-restore.sqlite')
    assert not (tmp_path / 'bad-restore.sqlite').exists()
    output.write_bytes(original)
    manifest = Path(str(output) + '.manifest.json')
    data = json.loads(manifest.read_text())
    data['sha256'] = 'ñ' * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(backup.BackupError, match='formato'):
        backup.verify_backup(output)


def test_backup_with_sidecars_or_missing_manifest_is_not_a_sealed_snapshot(database, tmp_path):
    source, _ = database
    output = tmp_path / 'snapshot.sqlite'
    backup.create_backup(source, output)
    sidecar = Path(str(output) + '-wal')
    sidecar.write_bytes(b'untrusted-wal')
    with pytest.raises(backup.BackupError, match='auxiliares'):
        backup.verify_backup(output)
    sidecar.unlink()
    Path(str(output) + '.manifest.json').unlink()
    with pytest.raises(backup.BackupError, match='debe existir'):
        backup.verify_backup(output)


def test_symlinks_missing_files_and_foreign_schema_leave_no_output(database, tmp_path):
    source, _ = database
    link = tmp_path / 'source-link.sqlite'
    link.symlink_to(source)
    with pytest.raises(backup.BackupError, match='enlace'):
        backup.create_backup(link, tmp_path / 'copy.sqlite')
    with pytest.raises(backup.BackupError, match='debe existir'):
        backup.create_backup(tmp_path / 'missing.sqlite', tmp_path / 'copy.sqlite')
    assert not (tmp_path / 'missing.sqlite').exists()
    foreign = tmp_path / 'unrelated.sqlite'
    with sqlite3.connect(foreign) as db:
        db.execute('CREATE TABLE unrelated(id INTEGER PRIMARY KEY)')
    with pytest.raises(backup.BackupError, match='compatible'):
        backup.create_backup(foreign, tmp_path / 'copy.sqlite')
    assert not (tmp_path / 'copy.sqlite').exists()
    assert not list(tmp_path.glob('.egasis-snapshot-*'))


def test_valid_unversioned_egasis_can_be_backed_up_before_schema_adoption(tmp_path):
    source = tmp_path / 'earlier-release.sqlite'
    engine = create_engine('sqlite:///' + str(source))
    from egasis.schema import baseline_metadata
    # Freeze the historical unversioned format instead of using the live ORM.
    baseline_metadata().create_all(engine)
    engine.dispose()
    output = tmp_path / 'before-adoption.sqlite'
    result = backup.create_backup(source, output)
    assert result['schema_version'] is None and result['schema_state'] == 'unversioned'
    assert backup.verify_backup(output)['valid']
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='_egasis_schema_migrations'").fetchone() is None


def test_publication_race_does_not_replace_or_remove_another_file(database, tmp_path, monkeypatch):
    source, _ = database
    output = tmp_path / 'snapshot.sqlite'
    original = backup._publish
    def racing_publish(temporary, target):
        target.write_bytes(b'created-by-other-process')
        original(temporary, target)
    monkeypatch.setattr(backup, '_publish', racing_publish)
    with pytest.raises(backup.BackupError, match='apareció'):
        backup.create_backup(source, output)
    assert output.read_bytes() == b'created-by-other-process'
    assert not list(tmp_path.glob('.egasis-snapshot-*'))


def test_cli_uses_only_explicit_paths_and_prints_metadata_without_credentials(database, tmp_path, capsys, monkeypatch):
    source, _ = database
    monkeypatch.setenv('EGASIS_DATABASE_URL', 'sqlite:////must-not-be-opened.sqlite')
    output = tmp_path / 'cli-snapshot.sqlite'
    assert backup.main(['create', '--source', str(source), '--output', str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['backup'] == str(output)
    assert backup.main(['verify', '--backup', str(output)]) == 0
    assert json.loads(capsys.readouterr().out)['valid']
    restored = tmp_path / 'cli-recovered.sqlite'
    assert backup.main(['restore', '--backup', str(output), '--output', str(restored)]) == 0
    captured = capsys.readouterr().out
    assert 'encrypted-fixture-placeholder' not in captured and 'fixture-session-only' not in captured
    assert json.loads(captured)['restored'] == str(restored)
