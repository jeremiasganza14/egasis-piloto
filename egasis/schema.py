"""Versioned, explicit schema upgrades for SQLite and PostgreSQL.

The baseline is frozen here rather than derived from changing ORM models.
Startup may initialize an empty database; an existing database is only upgraded
by upgrade_schema() or the --upgrade CLI. Stop API/workers before upgrading.
"""
import argparse
import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import (Column, Float, ForeignKey, Index, Integer, MetaData,
                        String, Table, Text, UniqueConstraint, create_engine,
                        inspect, select, text)
from sqlalchemy.schema import CreateColumn


class SchemaError(RuntimeError):
    pass


class MigrationRequired(SchemaError):
    pass


CURRENT_VERSION = 3
MANIFEST_TABLE = '_egasis_schema_migrations'

# Syntax: name:type, ! = NOT NULL, * = primary key; sN = VARCHAR(N).
# No Python-side ORM defaults are required for DDL. This is the original
# workspace schema, before persisted IMAP cursors and mail reservations.
_BASELINE = {
    'workspaces': 'id:integer* name:s160! offer:text audience:text signature:text timezone:s80 daily_budget:float plan:s30 subscription_status:s30 stripe_customer:s100 stripe_subscription:s100 created_at:float',
    'users': 'id:integer* workspace_id:integer! email:s254! password_hash:text! role:s20',
    'sessions': 'token_hash:s64* user_id:integer! expires_at:float!',
    'campaigns': 'id:integer* workspace_id:integer! name:s160! offer:text audience:text subject:text! body:text! status:s20 daily_limit:integer start_hour:integer end_hour:integer weekdays:s30 reply_mode:s20 created_at:float',
    'accounts': 'id:integer* workspace_id:integer! email:s254! display_name:s160 secret:text! smtp_host:s254 smtp_port:integer imap_host:s254 imap_port:integer daily_limit:integer cooldown_seconds:integer last_sent_at:float active:boolean last_error:text',
    'contacts': 'id:integer* workspace_id:integer! campaign_id:integer! account_id:integer email:s254! name:s160 company:s200 website:text source:s100 evidence:text score:integer fit_reason:text status:s30 created_at:float',
    'messages': 'id:integer* workspace_id:integer! contact_id:integer! account_id:integer direction:s20! subject:text! body:text! status:s30 classification:s30 provider_id:s255 in_reply_to:s255 idempotency_key:s255 error:text created_at:float sent_at:float',
    'jobs': 'id:integer* workspace_id:integer! message_id:integer! status:s30 attempts:integer due_at:float lease_until:float error:text',
    'suppressions': 'id:integer* workspace_id:integer! email:s254! reason:s100! created_at:float',
    'meetings': 'id:integer* workspace_id:integer! contact_id:integer! starts_at:float! duration_minutes:integer status:s30 location:text notes:text external_id:s255',
    'usage': 'id:integer* workspace_id:integer! contact_id:integer operation:s80! model:s80! input_tokens:integer output_tokens:integer cost:float created_at:float',
    'events': 'id:integer* workspace_id:integer! kind:s80! detail:text created_at:float',
    'webhook_events': 'id:s255* created_at:float',
    'connections': 'id:integer* workspace_id:integer! provider:s50! secret:text!',
}
_FOREIGN = {
    table: {'workspace_id': 'workspaces.id'}
    for table in ('users', 'campaigns', 'accounts', 'contacts', 'messages', 'jobs',
                  'suppressions', 'meetings', 'usage', 'events', 'connections')
}
_FOREIGN['sessions'] = {'user_id': 'users.id'}
_FOREIGN['contacts'].update(campaign_id='campaigns.id', account_id='accounts.id')
_FOREIGN['messages'].update(contact_id='contacts.id', account_id='accounts.id')
_FOREIGN['jobs']['message_id'] = 'messages.id'
_FOREIGN['meetings']['contact_id'] = 'contacts.id'
_FOREIGN['usage']['contact_id'] = 'contacts.id'
_UNIQUE = {
    'workspaces': [('stripe_customer',)], 'users': [('email',)],
    'accounts': [('workspace_id', 'email')],
    'contacts': [('workspace_id', 'campaign_id', 'email')],
    'messages': [('workspace_id', 'idempotency_key'), ('account_id', 'provider_id')],
    'jobs': [('message_id',)], 'suppressions': [('workspace_id', 'email')],
    'connections': [('workspace_id', 'provider')],
}
_INDEXES = {table: ['workspace_id'] for table in _FOREIGN if table != 'sessions'}
_INDEXES['contacts'].append('campaign_id')
_INDEXES['messages'].append('contact_id')
_ADDITIONS_V2 = {
    'accounts': [('imap_uidvalidity:s80', "''"), ('imap_last_uid:integer', '0')],
    'jobs': [('reserved_at:float', '0')],
    'campaigns': [('auto_reply_body:text', "''")],
}
_TABLES_V3 = {
    'knowledge_notes': 'id:integer* workspace_id:integer! text:text! status:s20! source_message_id:integer created_at:float! updated_at:float! approved_at:float archived_at:float',
}
_FOREIGN_V3 = {'knowledge_notes': {'workspace_id': 'workspaces.id', 'source_message_id': 'messages.id'}}
_INDEXES_V3 = {'knowledge_notes': ['workspace_id', 'source_message_id']}


def _column(spec, foreign=None, default=None):
    from sqlalchemy import Boolean
    name, kind = spec.split(':', 1)
    primary = kind.endswith('*')
    nullable = not (primary or kind.endswith('!'))
    kind = kind.rstrip('*!')
    types = {'integer': Integer, 'float': Float, 'text': Text, 'boolean': Boolean}
    datatype = String(int(kind[1:])) if kind.startswith('s') else types[kind]()
    args = [ForeignKey(foreign)] if foreign else []
    return Column(name, datatype, *args, primary_key=primary, nullable=nullable,
                  server_default=text(default) if default is not None else None)


def baseline_metadata():
    metadata = MetaData()
    for name, specs in _BASELINE.items():
        cols = [_column(spec, _FOREIGN.get(name, {}).get(spec.split(':')[0])) for spec in specs.split()]
        table = Table(name, metadata, *cols, *(UniqueConstraint(*cols) for cols in _UNIQUE.get(name, [])))
        for column in _INDEXES.get(name, []):
            Index(f'ix_{name}_{column}', table.c[column])
    return metadata


def metadata_for_version(version):
    metadata = baseline_metadata()
    if version >= 2:
        for table_name, additions in _ADDITIONS_V2.items():
            for spec, default in additions:
                metadata.tables[table_name].append_column(_column(spec, default=default))
    if version >= 3:
        for name, specs in _TABLES_V3.items():
            cols = [_column(spec, _FOREIGN_V3[name].get(spec.split(':')[0])) for spec in specs.split()]
            table = Table(name, metadata, *cols)
            for column in _INDEXES_V3[name]:
                Index(f'ix_{name}_{column}', table.c[column])
    return metadata


def _manifest():
    return Table(MANIFEST_TABLE, MetaData(),
                 Column('version', Integer, primary_key=True),
                 Column('name', String(120), nullable=False),
                 Column('checksum', String(64), nullable=False),
                 Column('applied_at', Float, nullable=False))


def _checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


MIGRATIONS = {
    1: {'name': 'workspace_baseline', 'checksum': _checksum([_BASELINE, _FOREIGN, _UNIQUE, _INDEXES])},
    2: {'name': 'durable_mail_state_and_approved_reply', 'checksum': _checksum(_ADDITIONS_V2)},
    3: {'name': 'reviewed_workspace_knowledge', 'checksum': _checksum([_TABLES_V3, _FOREIGN_V3, _INDEXES_V3])},
}


def _check_dialect(engine):
    if engine.dialect.name not in {'sqlite', 'postgresql'}:
        raise SchemaError('Las migraciones soportan SQLite y PostgreSQL.')


@contextmanager
def _locked_connection(engine):
    _check_dialect(engine)
    with engine.connect() as conn:
        if conn.dialect.name == 'sqlite':
            # Explicit BEGIN makes SQLite DDL transactional and serializes
            # simultaneous web/worker initialization before any schema reads.
            conn.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            conn.begin()
            conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 454741534953})
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _version(conn):
    tables = set(inspect(conn).get_table_names())
    if MANIFEST_TABLE not in tables:
        return (0, 'empty') if not tables else (None, 'unversioned')
    manifest = _manifest()
    actual_columns = {c['name'] for c in inspect(conn).get_columns(MANIFEST_TABLE)}
    if actual_columns != set(manifest.c.keys()):
        raise SchemaError('El manifiesto de migraciones no tiene el formato esperado.')
    rows = conn.execute(select(manifest).order_by(manifest.c.version)).mappings().all()
    if not rows:
        raise SchemaError('El manifiesto de migraciones está vacío; se requiere revisar la base.')
    versions = [row['version'] for row in rows]
    if versions != list(range(1, len(rows) + 1)):
        raise SchemaError('El historial de migraciones tiene versiones faltantes o desconocidas.')
    for row in rows:
        expected = MIGRATIONS.get(row['version'])
        if not expected:
            raise SchemaError('La base usa una versión más nueva que esta aplicación.')
        if row['name'] != expected['name'] or row['checksum'] != expected['checksum']:
            raise SchemaError('El historial de migraciones no coincide con esta aplicación.')
    return versions[-1], 'current' if versions[-1] == CURRENT_VERSION else 'upgrade_required'


def _compatible_type(actual, expected):
    if actual._type_affinity is not expected._type_affinity:
        return False
    if isinstance(expected, String) and not isinstance(expected, Text):
        return getattr(actual, 'length', None) == expected.length
    return True


def _validate_schema(conn, version, allow_known_additions=False):
    inspector = inspect(conn)
    actual_tables = set(inspector.get_table_names()) - {MANIFEST_TABLE}
    expected_metadata = metadata_for_version(version)
    if actual_tables != set(expected_metadata.tables):
        raise SchemaError('La base no coincide con el esquema de Egasis: faltan o sobran tablas.')
    for table_name, table in expected_metadata.tables.items():
        expected_columns = {c.name: c for c in table.c}
        optional = {spec.split(':')[0]: _column(spec) for spec, _ in _ADDITIONS_V2.get(table_name, [])}
        if version >= 2:
            expected_columns.update(optional)
        columns = {c['name']: c for c in inspector.get_columns(table_name)}
        extras = set(columns) - set(expected_columns)
        if (set(expected_columns) - set(columns)) or (extras - set(optional) if allow_known_additions else extras):
            raise SchemaError(f'Las columnas de {table_name} no coinciden con la versión registrada.')
        check_columns = {**expected_columns, **{name: optional[name] for name in extras}}
        for name, expected in check_columns.items():
            actual = columns[name]
            if not _compatible_type(actual['type'], expected.type) or actual['nullable'] != expected.nullable:
                raise SchemaError(f'La definición de {table_name}.{name} no coincide con la versión registrada.')
        primary = set(inspector.get_pk_constraint(table_name)['constrained_columns'])
        if primary != {c.name for c in table.primary_key}:
            raise SchemaError(f'La clave primaria de {table_name} no coincide.')
        unique = {tuple(c['column_names']) for c in inspector.get_unique_constraints(table_name)}
        unique |= {tuple(c['column_names']) for c in inspector.get_indexes(table_name) if c['unique']}
        if not set(_UNIQUE.get(table_name, [])).issubset(unique):
            raise SchemaError(f'Faltan restricciones de unicidad en {table_name}.')
        foreign = {(tuple(fk['constrained_columns']), fk['referred_table'], tuple(fk['referred_columns']))
                   for fk in inspector.get_foreign_keys(table_name)}
        for name, reference in {**_FOREIGN, **_FOREIGN_V3}.get(table_name, {}).items():
            target_table, target_column = reference.split('.')
            if ((name,), target_table, (target_column,)) not in foreign:
                raise SchemaError(f'Falta la relación de {table_name}.{name}.')


def _result(version, state):
    return {'version': version, 'latest': CURRENT_VERSION, 'state': state,
            'pending': list(range((version or 0) + 1, CURRENT_VERSION + 1))}


def schema_status(engine):
    """Inspect without DDL. An absent SQLite file is not created by this call."""
    _check_dialect(engine)
    database = engine.url.database
    if engine.dialect.name == 'sqlite' and database and database != ':memory:' and not Path(database).exists():
        return _result(0, 'empty')
    with engine.connect() as conn:
        version, state = _version(conn)
        if version:
            _validate_schema(conn, version)
        return _result(version, state)


def upgrade_schema(engine, *, target_version=CURRENT_VERSION, adopt_unversioned=False):
    """Apply ordered migrations transactionally; never downgrade a database.

    Adoption is explicit and only accepts a complete known Egasis baseline, with
    optional already-present v2 fields from the earlier unversioned release.
    """
    if target_version not in MIGRATIONS:
        raise SchemaError('Versión de destino desconocida.')
    with _locked_connection(engine) as conn:
        version, state = _version(conn)
        if state == 'unversioned':
            if not adopt_unversioned:
                raise MigrationRequired('La base no tiene versión. Revisá y ejecutá --upgrade --adopt-unversioned.')
            _validate_schema(conn, 1, allow_known_additions=True)
            if target_version < CURRENT_VERSION:
                raise SchemaError('La adopción de una base existente requiere la versión actual.')
            _manifest().create(conn)
            conn.execute(_manifest().insert().values(version=1, applied_at=time.time(), **MIGRATIONS[1]))
            version = 1
        elif version:
            _validate_schema(conn, version)
        if version > target_version:
            raise SchemaError('No se permite bajar la versión de la base.')
        for revision in range(version + 1, target_version + 1):
            if revision == 1:
                baseline_metadata().create_all(conn)
                _manifest().create(conn)
            elif revision == 2:
                for table_name, additions in _ADDITIONS_V2.items():
                    present = {c['name'] for c in inspect(conn).get_columns(table_name)}
                    for spec, default in additions:
                        column = _column(spec, default=default)
                        if column.name not in present:
                            definition = str(CreateColumn(column).compile(dialect=conn.dialect))
                            quoted = conn.dialect.identifier_preparer.quote_identifier(table_name)
                            conn.exec_driver_sql(f'ALTER TABLE {quoted} ADD COLUMN {definition}')
            elif revision == 3:
                metadata = metadata_for_version(3)
                for table_name in _TABLES_V3:
                    metadata.tables[table_name].create(conn)
            _validate_schema(conn, revision)
            conn.execute(_manifest().insert().values(version=revision, applied_at=time.time(), **MIGRATIONS[revision]))
        return _result(target_version, 'current' if target_version == CURRENT_VERSION else 'upgrade_required')


def ensure_schema(engine):
    """Startup contract: initialize an empty database, reject implicit upgrades."""
    status = schema_status(engine)
    if status['state'] == 'empty':
        return upgrade_schema(engine)
    if status['state'] != 'current':
        raise MigrationRequired('La base necesita una migración explícita: python -m egasis.schema --database-url URL --upgrade (agregá --adopt-unversioned si no tiene versión).')
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description='Estado y migraciones de la nueva base de Egasis. Detené servidor y worker antes de actualizar.')
    parser.add_argument('--database-url', required=True)
    parser.add_argument('--upgrade', action='store_true', help='Aplica las migraciones hasta la versión actual.')
    parser.add_argument('--adopt-unversioned', action='store_true', help='Adopta una base Egasis anterior sin manifiesto, tras validar su estructura.')
    args = parser.parse_args(argv)
    if args.adopt_unversioned and not args.upgrade:
        parser.error('--adopt-unversioned requiere --upgrade.')
    engine = create_engine(args.database_url)
    try:
        result = upgrade_schema(engine, adopt_unversioned=args.adopt_unversioned) if args.upgrade else schema_status(engine)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except SchemaError as exc:
        parser.exit(2, f'No se actualizó el esquema: {exc}\n')
    finally:
        engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
