from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateColumn, CreateTable

from egasis import schema
from egasis.models import Base


@pytest.fixture
def engine(tmp_path):
    value = create_engine('sqlite:///' + str(tmp_path / 'schema.db'))
    yield value
    value.dispose()


def fields(engine, name):
    return {c['name'] for c in inspect(engine).get_columns(name)}


def test_empty_database_bootstrap_has_manifest_and_matches_models(engine):
    result = schema.ensure_schema(engine)
    assert result == {'version': schema.CURRENT_VERSION, 'latest': schema.CURRENT_VERSION, 'state': 'current', 'pending': []}
    assert set(inspect(engine).get_table_names()) == set(Base.metadata.tables) | {schema.MANIFEST_TABLE}
    for name, table in Base.metadata.tables.items():
        assert fields(engine, name) == set(table.c.keys())
    with engine.connect() as conn:
        versions = conn.execute(text('SELECT version, name, checksum FROM _egasis_schema_migrations ORDER BY version')).all()
    assert [row[0] for row in versions] == list(range(1, schema.CURRENT_VERSION + 1))
    assert all(len(row[2]) == 64 for row in versions)
    assert schema.ensure_schema(engine) == result


def test_v1_requires_explicit_upgrade_and_preserves_existing_rows(engine):
    schema.upgrade_schema(engine, target_version=1)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO workspaces (id,name) VALUES (1,'Existing workspace')"))
        conn.execute(text("INSERT INTO accounts (id,workspace_id,email,secret) VALUES (1,1,'mail@example.com','encrypted')"))
        conn.execute(text("INSERT INTO campaigns (id,workspace_id,name,subject,body) VALUES (1,1,'Campaign','Subject','Body')"))
    with pytest.raises(schema.MigrationRequired):
        schema.ensure_schema(engine)
    assert 'imap_last_uid' not in fields(engine, 'accounts')
    assert schema.schema_status(engine)['version'] == 1
    assert schema.upgrade_schema(engine)['version'] == schema.CURRENT_VERSION
    with engine.connect() as conn:
        assert conn.execute(text('SELECT name FROM workspaces')).scalar() == 'Existing workspace'
        assert tuple(conn.execute(text('SELECT imap_uidvalidity,imap_last_uid FROM accounts')).one()) == ('', 0)
        assert conn.execute(text('SELECT auto_reply_body FROM campaigns')).scalar() == ''
    assert 'reserved_at' in fields(engine, 'jobs')


@pytest.mark.parametrize('current_shape', [False, True])
def test_existing_unversioned_database_needs_explicit_adoption(engine, current_shape):
    (schema.metadata_for_version(2) if current_shape else schema.baseline_metadata()).create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO workspaces (id,name) VALUES (1,'Preserved')"))
    assert schema.schema_status(engine)['state'] == 'unversioned'
    with pytest.raises(schema.MigrationRequired):
        schema.ensure_schema(engine)
    with pytest.raises(schema.MigrationRequired):
        schema.upgrade_schema(engine)
    assert schema.MANIFEST_TABLE not in inspect(engine).get_table_names()
    assert schema.upgrade_schema(engine, adopt_unversioned=True)['version'] == schema.CURRENT_VERSION
    with engine.connect() as conn:
        assert conn.execute(text('SELECT name FROM workspaces')).scalar() == 'Preserved'


def test_future_version_fails_without_rewriting_manifest(engine):
    schema.ensure_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO _egasis_schema_migrations VALUES (:version,'future','future-checksum',0)"), {'version': schema.CURRENT_VERSION + 1})
    for action in (schema.schema_status, schema.ensure_schema, schema.upgrade_schema):
        with pytest.raises(schema.SchemaError, match='más nueva'):
            action(engine)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT MAX(version) FROM _egasis_schema_migrations')).scalar() == schema.CURRENT_VERSION + 1


def test_tampered_or_missing_revision_fails(engine):
    schema.ensure_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE _egasis_schema_migrations SET checksum='changed' WHERE version=1"))
    with pytest.raises(schema.SchemaError, match='no coincide'):
        schema.upgrade_schema(engine)
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM _egasis_schema_migrations WHERE version=1'))
    with pytest.raises(schema.SchemaError, match='faltantes'):
        schema.ensure_schema(engine)


def test_partial_or_foreign_schema_is_not_stamped(engine):
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE leads (id INTEGER PRIMARY KEY, email TEXT)'))
    with pytest.raises(schema.SchemaError, match='faltan o sobran tablas'):
        schema.upgrade_schema(engine, adopt_unversioned=True)
    assert inspect(engine).get_table_names() == ['leads']


def test_missing_tenant_unique_constraint_is_not_adopted(engine):
    metadata = schema.baseline_metadata()
    accounts = metadata.tables['accounts']
    accounts.constraints = {c for c in accounts.constraints if c.__class__.__name__ != 'UniqueConstraint'}
    metadata.create_all(engine)
    with pytest.raises(schema.SchemaError, match='unicidad'):
        schema.upgrade_schema(engine, adopt_unversioned=True)
    assert schema.MANIFEST_TABLE not in inspect(engine).get_table_names()


def test_failed_upgrade_rolls_back_ddl_and_manifest(engine, monkeypatch):
    schema.upgrade_schema(engine, target_version=1)
    validate = schema._validate_schema

    def fail_revision_two(conn, version, **kwargs):
        validate(conn, version, **kwargs)
        if version == 2:
            raise schema.SchemaError('injected verification failure')

    monkeypatch.setattr(schema, '_validate_schema', fail_revision_two)
    with pytest.raises(schema.SchemaError, match='injected'):
        schema.upgrade_schema(engine)
    assert 'imap_last_uid' not in fields(engine, 'accounts')
    assert 'auto_reply_body' not in fields(engine, 'campaigns')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT MAX(version) FROM _egasis_schema_migrations')).scalar() == 1


def test_no_downgrade_or_unknown_target(engine):
    schema.ensure_schema(engine)
    with pytest.raises(schema.SchemaError, match='bajar'):
        schema.upgrade_schema(engine, target_version=1)
    with pytest.raises(schema.SchemaError, match='desconocida'):
        schema.upgrade_schema(engine, target_version=999)


def test_schema_drift_is_not_silently_repaired(engine):
    schema.ensure_schema(engine)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE workspaces ADD COLUMN surprise TEXT'))
    with pytest.raises(schema.SchemaError, match='columnas'):
        schema.ensure_schema(engine)


def test_status_cli_is_read_only_for_missing_file(tmp_path, capsys):
    path = tmp_path / 'not-created.db'
    assert schema.main(['--database-url', 'sqlite:///' + str(path)]) == 0
    assert '"state": "empty"' in capsys.readouterr().out
    assert not path.exists()
    with pytest.raises(SystemExit):
        schema.main(['--database-url', 'sqlite:///' + str(path), '--adopt-unversioned'])
    assert not path.exists()


def test_concurrent_initialization_is_serialized(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'concurrent.db')

    def initialize(_):
        database = create_engine(url, connect_args={'timeout': 10})
        try:
            return schema.ensure_schema(database)
        finally:
            database.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(initialize, range(2)))
    assert all(result['version'] == schema.CURRENT_VERSION for result in results)


def test_postgresql_ddl_compiles_without_sqlite_specific_types():
    dialect = postgresql.dialect()
    metadata = schema.baseline_metadata()
    definitions = {name: str(CreateTable(table).compile(dialect=dialect)) for name, table in metadata.tables.items()}
    assert 'SERIAL' in definitions['workspaces']
    assert 'BOOLEAN' in definitions['accounts']
    assert 'FOREIGN KEY' in definitions['contacts']
    assert 'UNIQUE' in definitions['messages']
    for additions in schema._ADDITIONS_V2.values():
        for spec, default in additions:
            compiled = str(CreateColumn(schema._column(spec, default=default)).compile(dialect=dialect))
            assert 'DEFAULT' in compiled


def test_v3_adds_knowledge_without_changing_frozen_migrations(engine):
    assert schema.MIGRATIONS[1]['checksum'] == '6d5877cdf6f41318896143d9137e3b923ecc34c0d9f72db237a7d04c736f81f4'
    assert schema.MIGRATIONS[2]['checksum'] == 'c630c90857db47e78ea5d10ea73584efbcec53b81a65fa908f6e30f8554ef82a'
    schema.upgrade_schema(engine, target_version=2)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO workspaces (id,name) VALUES (1,'Preserved through v3')"))
    assert 'knowledge_notes' not in inspect(engine).get_table_names()
    with pytest.raises(schema.MigrationRequired):
        schema.ensure_schema(engine)
    assert schema.upgrade_schema(engine)['version'] == 3
    with engine.connect() as conn:
        assert conn.execute(text('SELECT name FROM workspaces')).scalar() == 'Preserved through v3'
        assert conn.execute(text('SELECT COUNT(*) FROM knowledge_notes')).scalar() == 0
    assert fields(engine, 'knowledge_notes') == set(Base.metadata.tables['knowledge_notes'].c.keys())
    definition = str(CreateTable(schema.metadata_for_version(3).tables['knowledge_notes']).compile(dialect=postgresql.dialect()))
    assert 'FOREIGN KEY(source_message_id) REFERENCES messages (id)' in definition
