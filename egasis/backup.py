"""Consistent SQLite snapshots and conservative restoration to a NEW file only.

Examples (all paths must be explicit):
    python -m egasis.backup create --source data/egasis.db --output backups/egasis.sqlite
    python -m egasis.backup verify --backup backups/egasis.sqlite
    python -m egasis.backup restore --backup backups/egasis.sqlite --output data/recovered.sqlite

Online creation uses SQLite's backup API, including committed WAL data. A backup
is a private SQLite file plus a SHA-256 manifest; neither operation reads or
exports environment files or encryption keys. Retain EGASIS_SECRET_KEY separately
in its existing secure storage: encrypted account credentials need that same key.

Restore never replaces a live database. Stop API/workers before switching their
database path to the new restored file. Restore disables accounts, pauses active
campaigns, revokes sessions and holds potentially replayable work for review;
events after the snapshot cannot be recovered from this backup alone. Verify
provider records before resolving held deliveries or reactivating campaigns.
"""
import argparse
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import quote

from sqlalchemy import create_engine

from .schema import SchemaError, _validate_schema, schema_status


FORMAT = 'egasis-sqlite-backup/v1'
RECOVERY_ERROR = 'Restored snapshot: provider records must be reconciled before any retry'


class BackupError(RuntimeError):
    pass


def _source(path):
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise BackupError('El archivo de origen debe existir y no puede ser un enlace simbólico.')
    return candidate.resolve(strict=True)


def _destination(path):
    candidate = Path(path).expanduser()
    if os.path.lexists(candidate):
        raise BackupError('El destino ya existe. Elegí un archivo nuevo; nunca se sobrescriben bases ni copias.')
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError:
        raise BackupError('La carpeta de destino debe existir.') from None
    if not parent.is_dir():
        raise BackupError('La carpeta de destino no es válida.')
    resolved = parent / candidate.name
    if any(os.path.lexists(str(resolved) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise BackupError('El destino tiene archivos auxiliares SQLite. Elegí otro nombre.')
    return resolved


def _connect(path, *, immutable=False):
    uri = 'file:' + quote(str(path), safe='/') + '?mode=ro' + ('&immutable=1' if immutable else '')
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA trusted_schema=OFF')
    return connection


def _validate(path):
    try:
        with closing(_connect(path, immutable=True)) as connection:
            if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise BackupError('La copia no supera la verificación de integridad SQLite.')
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise BackupError('La copia contiene relaciones inválidas entre registros.')
        engine = create_engine('sqlite://', creator=lambda: _connect(path, immutable=True))
        try:
            result = schema_status(engine)
            if result['state'] == 'empty':
                raise BackupError('El archivo no contiene una base Egasis.')
            if result['state'] == 'unversioned':
                # Permit a valid earlier Egasis database to be backed up BEFORE
                # explicit schema adoption; do not adopt or modify it here.
                with engine.connect() as connection:
                    _validate_schema(connection, 1, allow_known_additions=True)
            return result
        finally:
            engine.dispose()
    except (sqlite3.Error, SchemaError):
        raise BackupError('El archivo no es una base Egasis íntegra y compatible.') from None


def _temporary(parent):
    descriptor, name = tempfile.mkstemp(prefix='.egasis-snapshot-', dir=parent)
    os.close(descriptor)
    os.chmod(name, 0o600)
    return Path(name)


def _digest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _sync_file(path):
    with path.open('rb') as stream:
        os.fsync(stream.fileno())


def _publish(temporary, target):
    # Hard-link publication is atomic and fails if ANY destination exists;
    # os.replace would silently overwrite a file created by another process.
    try:
        os.link(temporary, target)
    except FileExistsError:
        raise BackupError('El destino apareció durante la operación. No se sobrescribió.') from None


def _remove_own_link(target, temporary):
    try:
        if target.exists() and os.path.samefile(target, temporary):
            target.unlink()
    except OSError:
        pass


def _snapshot(source, target):
    started = time.monotonic()
    def progress(status, remaining, total):
        if time.monotonic() - started > 30:
            raise BackupError('SQLite no pudo completar la copia en 30 segundos. Reintentá cuando haya menos actividad.')
    reader = _connect(source)
    writer = sqlite3.connect(target, timeout=5)
    try:
        reader.backup(writer, pages=128, progress=progress, sleep=0.05)
        writer.commit()
        writer.execute('PRAGMA journal_mode=DELETE')
    finally:
        writer.close()
        reader.close()


def create_backup(source_path, output_path):
    """Create new snapshot + .manifest.json; return paths/checksum/version only."""
    source, output = _source(source_path), _destination(output_path)
    manifest_path = _destination(str(output) + '.manifest.json')
    temporary, manifest_temp = _temporary(output.parent), _temporary(output.parent)
    try:
        _snapshot(source, temporary)
        schema = _validate(temporary)
        checksum, size = _digest(temporary)
        manifest = {'format': FORMAT, 'created_at': time.time(), 'sha256': checksum,
            'bytes': size, 'schema_version': schema['version'], 'schema_state': schema['state']}
        manifest_temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        _sync_file(temporary)
        _sync_file(manifest_temp)
        _publish(temporary, output)
        _publish(manifest_temp, manifest_path)
        return {'backup': str(output), 'manifest': str(manifest_path), **manifest}
    except Exception:
        _remove_own_link(output, temporary)
        _remove_own_link(manifest_path, manifest_temp)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        manifest_temp.unlink(missing_ok=True)


def verify_backup(backup_path):
    """Validate sealed snapshot checksum, SQLite integrity, FKs and schema."""
    backup = _source(backup_path)
    if any(os.path.lexists(str(backup) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise BackupError('La copia tiene archivos SQLite auxiliares. Debe ser una instantánea independiente.')
    manifest_path = _source(str(backup) + '.manifest.json')
    if manifest_path.stat().st_size > 65_536:
        raise BackupError('El manifiesto de la copia es demasiado grande.')
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError):
        raise BackupError('El manifiesto no se pudo interpretar.') from None
    if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or not isinstance(manifest.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', manifest['sha256']):
        raise BackupError('El manifiesto no tiene el formato esperado.')
    checksum, size = _digest(backup)
    if not hmac.compare_digest(checksum, manifest['sha256']) or size != manifest.get('bytes'):
        raise BackupError('La copia cambió o está incompleta: su checksum no coincide.')
    schema = _validate(backup)
    if schema['version'] != manifest.get('schema_version'):
        raise BackupError('La versión de esquema no coincide con el manifiesto.')
    return {'valid': True, 'backup': str(backup), 'manifest': str(manifest_path), **manifest}


def _hold_restored_work(path):
    """A snapshot cannot prove what was sent AFTER it was taken. Hold all replay."""
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA trusted_schema=OFF')
    now = time.time()
    try:
        connection.execute('BEGIN IMMEDIATE')
        counts = {}
        statements = {
            'campaigns_paused': ("UPDATE campaigns SET status='paused' WHERE status='active'", ()),
            'accounts_disabled': ('UPDATE accounts SET active=0 WHERE active=1', ()),
            'contacts_held': ("UPDATE contacts SET status='recovery_hold' WHERE status='pending'", ()),
            'inbound_review': ("UPDATE messages SET status='needs_review' WHERE direction='inbound' AND status='received'", ()),
            'messages_held': ("UPDATE messages SET status='uncertain', error=? WHERE direction='outbound' AND status NOT IN ('sent','simulated')", (RECOVERY_ERROR,)),
            'sessions_revoked': ('DELETE FROM sessions', ()),
        }
        for name, (sql, params) in statements.items():
            counts[name] = connection.execute(sql, params).rowcount
        connection.execute("UPDATE jobs SET status='uncertain',error=?,lease_until=0 WHERE message_id IN (SELECT id FROM messages WHERE direction='outbound' AND status='uncertain')", (RECOVERY_ERROR,))
        columns = {row[1] for row in connection.execute('PRAGMA table_info(jobs)')}
        fields = 'workspace_id,message_id,status,attempts,due_at,lease_until,error' + (',reserved_at' if 'reserved_at' in columns else '')
        values = "m.workspace_id,m.id,'uncertain',0,?,0,?" + (',0' if 'reserved_at' in columns else '')
        counts['jobs_created_for_review'] = connection.execute(f"INSERT INTO jobs ({fields}) SELECT {values} FROM messages m WHERE m.direction='outbound' AND m.status='uncertain' AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.message_id=m.id)", (now, RECOVERY_ERROR)).rowcount
        for (workspace_id,) in connection.execute('SELECT id FROM workspaces').fetchall():
            connection.execute('INSERT INTO events(workspace_id,kind,detail,created_at) VALUES(?,?,?,?)',
                (workspace_id, 'backup.restored', 'Base restaurada en un archivo nuevo. Cuentas desactivadas, campañas pausadas y envíos pendientes retenidos hasta verificar registros del proveedor.', now))
        connection.commit()
        return counts
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def restore_backup(backup_path, output_path):
    """Restore only to a nonexistent path, holding all potentially replayable work."""
    verified = verify_backup(backup_path)
    backup, output = Path(verified['backup']), _destination(output_path)
    temporary = _temporary(output.parent)
    try:
        # Copy the sealed, self-contained snapshot and hash THIS copy too, closing
        # the gap if the source file changes after verification.
        with backup.open('rb') as reader, temporary.open('wb') as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b''):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        checksum, size = _digest(temporary)
        if not hmac.compare_digest(checksum, verified['sha256']) or size != verified['bytes']:
            raise BackupError('La copia de origen cambió durante la restauración.')
        counts = _hold_restored_work(temporary)
        schema = _validate(temporary)
        _sync_file(temporary)
        _publish(temporary, output)
        return {'restored': str(output), 'source_backup': str(backup), 'schema_version': schema['version'],
            'schema_state': schema['state'], 'safety': counts,
            'next_step': 'Detené API y worker antes de cambiar a este archivo. Conservá la clave original por separado y conciliá los envíos retenidos antes de reactivar.'}
    except Exception:
        _remove_own_link(output, temporary)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Copias SQLite consistentes de Egasis; destinos nuevos, sin cargar configuración ni claves.')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='Crear instantánea SQLite y manifiesto de verificación.')
    create.add_argument('--source', required=True)
    create.add_argument('--output', required=True)
    verify = commands.add_parser('verify', help='Verificar checksum, integridad y esquema.')
    verify.add_argument('--backup', required=True)
    restore = commands.add_parser('restore', help='Restaurar a un archivo nuevo con envíos retenidos.')
    restore.add_argument('--backup', required=True)
    restore.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'create':
            result = create_backup(args.source, args.output)
        elif args.command == 'verify':
            result = verify_backup(args.backup)
        else:
            result = restore_backup(args.backup, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BackupError, OSError, sqlite3.Error) as exc:
        message = str(exc) if isinstance(exc, BackupError) else 'No se pudo completar la operación de archivos o SQLite.'
        parser.exit(2, message + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
