import json
import socket
import os
import re
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from egasis.deployment_check import main, readiness
from egasis.settings import Settings, normalize_database_url


@pytest.fixture
def production_env():
    return {'EGASIS_ENV': 'production',
            'EGASIS_DATABASE_URL': 'postgres://egasis:ENCODED%40PASSWORD@postgres.railway.internal:5432/egasis',
            'EGASIS_SECRET_KEY': 'unique-deployment-secret-0123456789-abcdef',
            'EGASIS_PUBLIC_URL': 'https://egasis.example.com',
            'EGASIS_REGISTRATION_TOKEN': 'unique-invitation-token-0123456789-abcdef'}


def test_production_postgres_does_not_touch_local_files(production_env, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Production PostgreSQL must not access local key storage')
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    monkeypatch.setattr(Path, 'read_text', forbidden)
    config = Settings.from_env(production_env, data_dir=tmp_path / 'read-only-data')
    assert config.database_url.startswith('postgresql+psycopg2://')
    assert config.production is True
    assert config.simulation is True
    assert not (tmp_path / 'read-only-data').exists()


@pytest.mark.parametrize('scheme', ['postgres', 'postgresql', 'postgresql+psycopg2'])
def test_postgres_aliases_preserve_escaped_password_and_options(scheme):
    value = normalize_database_url(scheme + '://user:p%40ss%3Aword%2Fwith%25@database.example.com:5432/egasis?sslmode=require&connect_timeout=10')
    url = make_url(value)
    assert url.drivername == 'postgresql+psycopg2'
    assert url.password == 'p@ss:word/with%'
    assert url.query == {'sslmode': 'require', 'connect_timeout': '10'}
    assert '***' not in value


def test_provider_database_url_fallback_and_explicit_precedence(production_env):
    original = production_env.pop('EGASIS_DATABASE_URL')
    production_env['DATABASE_URL'] = original
    assert Settings.from_env(production_env).database_url == normalize_database_url(original)
    production_env['EGASIS_DATABASE_URL'] = 'postgresql://own:secret@other.example.com/own'
    assert make_url(Settings.from_env(production_env).database_url).database == 'own'


@pytest.mark.parametrize('value', ['postgres://user:password@host', 'mysql://user:password@host/db',
                                  'postgresql+asyncpg://user:password@host/db', 'BAD_SECRET_DATABASE_VALUE'])
def test_invalid_database_errors_never_echo_credentials(production_env, value):
    production_env['EGASIS_DATABASE_URL'] = value
    with pytest.raises(RuntimeError) as error:
        Settings.from_env(production_env)
    assert value not in str(error.value)
    result = readiness(production_env)
    assert result['configuration_ready'] is False
    assert value not in json.dumps(result)


@pytest.mark.parametrize('changes', [
    {'EGASIS_ENV': 'prod'},
    {'EGASIS_SECRET_KEY': 'short'},
    {'EGASIS_SECRET_KEY': 'x' * 64},
    {'EGASIS_SECRET_KEY': ' unique-deployment-secret-0123456789-abcdef'},
    {'EGASIS_REGISTRATION_TOKEN': 'short-invite'},
    {'EGASIS_PUBLIC_URL': 'http://egasis.example.com'},
    {'EGASIS_PUBLIC_URL': 'https://user:PRIVATE_PASSWORD@egasis.example.com'},
    {'EGASIS_PUBLIC_URL': 'https://egasis.example.com/path'},
    {'EGASIS_PUBLIC_URL': 'https://egasis.example.com?secret=PRIVATE'},
    {'EGASIS_PUBLIC_URL': 'https://localhost:8765'},
    {'EGASIS_PUBLIC_URL': 'https://127.0.0.1:8765'},
    {'EGASIS_SIMULATION': 'flase'},
])
def test_invalid_production_settings_fail_before_any_write(production_env, changes, tmp_path):
    production_env.update(changes)
    root = tmp_path / 'must-not-be-created'
    with pytest.raises(RuntimeError):
        Settings.from_env(production_env, data_dir=root)
    assert not root.exists()


def test_production_requires_explicit_database(production_env):
    production_env.pop('EGASIS_DATABASE_URL')
    with pytest.raises(RuntimeError, match='DATABASE_URL'):
        Settings.from_env(production_env)


def test_closed_registration_is_a_supported_production_configuration(production_env):
    production_env.pop('EGASIS_REGISTRATION_TOKEN')
    assert Settings.from_env(production_env).registration_token == ''
    report = readiness(production_env)
    assert report['configuration_ready'] is True
    assert report['registration'] == 'closed'


def test_development_keeps_stable_local_key(tmp_path):
    root = tmp_path / 'data'
    one = Settings.from_env({}, data_dir=root)
    two = Settings.from_env({}, data_dir=root)
    assert one.secret_key == two.secret_key
    assert len(one.secret_key) >= 32
    assert (root / '.development-key').stat().st_mode & 0o777 == 0o600
    assert one.simulation is True


def test_readiness_does_not_open_database_or_network_or_write(production_env, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline check attempted an external operation')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    import psycopg2
    monkeypatch.setattr(psycopg2, 'connect', forbidden)
    result = readiness(production_env)
    assert result['configuration_ready'] is True
    assert result['scope'] == 'offline_configuration_only'
    serialized = json.dumps(result)
    for field in ('EGASIS_DATABASE_URL', 'EGASIS_SECRET_KEY', 'EGASIS_REGISTRATION_TOKEN', 'EGASIS_PUBLIC_URL'):
        assert production_env[field] not in serialized
    assert 'ENCODED' not in serialized


def test_readiness_detects_missing_postgres_driver(production_env):
    result = readiness(production_env, module_available=lambda module: False)
    assert result['configuration_ready'] is False
    assert any(item['field'] == 'psycopg2' for item in result['errors'])


def test_installed_postgres_driver_loads_without_connecting(production_env, monkeypatch):
    import psycopg2
    monkeypatch.setattr(psycopg2, 'connect', lambda *args, **kwargs: pytest.fail('Unexpected database connection'))
    engine = create_engine(Settings.from_env(production_env).database_url)
    assert engine.dialect.name == 'postgresql'
    assert engine.dialect.driver == 'psycopg2'
    engine.dispose()


def test_railway_real_smtp_requires_operator_declared_pro_plan(production_env):
    production_env['EGASIS_SIMULATION'] = 'false'
    for plan in (None, 'free', 'trial', 'hobby'):
        assert readiness(production_env, role='worker', railway_plan=plan)['configuration_ready'] is False
    allowed = readiness(production_env, role='worker', railway_plan='pro')
    assert allowed['configuration_ready'] is True
    assert any('no se verificó' in note for note in allowed['notes'])
    production_env['EGASIS_SIMULATION'] = 'true'
    assert readiness(production_env, role='worker')['configuration_ready'] is True


def test_vercel_requires_external_tls_postgres_and_external_worker(production_env):
    report = readiness(production_env, target='vercel')
    assert report['configuration_ready'] is False
    assert len([error for error in report['errors'] if error['field'] == 'EGASIS_DATABASE_URL']) == 2
    production_env['EGASIS_DATABASE_URL'] = 'postgresql://user:private@external.example.com:5432/egasis?sslmode=require'
    assert readiness(production_env, target='vercel')['configuration_ready'] is True
    assert readiness(production_env, target='vercel', role='worker')['configuration_ready'] is False


@pytest.mark.parametrize('target', ['railway', 'vercel', 'render'])
def test_separate_service_hosts_reject_container_sqlite_default(production_env, target):
    production_env['EGASIS_DATABASE_URL'] = 'sqlite:////app/data/egasis.db'
    assert readiness(production_env, target=target)['configuration_ready'] is False


def test_dataclass_repr_does_not_expose_configuration_secrets(production_env):
    config = Settings.from_env(production_env)
    assert config.secret_key not in repr(config)
    assert config.registration_token not in repr(config)
    assert 'PASSWORD' not in repr(config)


def test_cli_result_is_parseable_and_returns_failure_for_missing_config(monkeypatch, capsys):
    for key in ('EGASIS_ENV', 'EGASIS_DATABASE_URL', 'DATABASE_URL', 'EGASIS_SECRET_KEY', 'EGASIS_PUBLIC_URL', 'EGASIS_REGISTRATION_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    assert main(['--target', 'railway', '--role', 'worker']) == 2
    report = json.loads(capsys.readouterr().out)
    assert report['configuration_ready'] is False
    assert report['errors']


@pytest.fixture
def render_env(production_env):
    production_env.update({
        'EGASIS_DATABASE_URL': 'postgresql://egasis:PRIVATE_DATABASE_PASSWORD@ep-example.us-east-2.aws.neon.tech/egasis?sslmode=require',
        'EGASIS_PUBLIC_URL': 'https://egasis-piloto.onrender.com',
        'EGASIS_SIMULATION': 'true',
    })
    return production_env


def test_render_free_web_is_offline_and_secret_free(render_env, monkeypatch):
    import psycopg2
    monkeypatch.setattr(psycopg2, 'connect', lambda *args, **kwargs: pytest.fail('Unexpected connection'))
    result = readiness(render_env, target='render')
    assert result['configuration_ready'] is True
    assert result['simulation'] is True
    assert result['target'] == 'render'
    serialized = json.dumps(result)
    for value in (render_env['EGASIS_SECRET_KEY'], render_env['EGASIS_DATABASE_URL'],
                  render_env['EGASIS_REGISTRATION_TOKEN'], 'PRIVATE_DATABASE_PASSWORD'):
        assert value not in serialized


@pytest.mark.parametrize('host', ['localhost', '127.0.0.1', '10.1.2.3', '[::1]',
                                 'postgres.railway.internal', 'database.local', 'postgres'])
def test_render_rejects_private_database_addresses(render_env, host):
    render_env['EGASIS_DATABASE_URL'] = f'postgresql://user:private@{host}/egasis?sslmode=require'
    result = readiness(render_env, target='render')
    assert result['configuration_ready'] is False
    assert any(error['field'] == 'EGASIS_DATABASE_URL' for error in result['errors'])


@pytest.mark.parametrize('sslmode', [None, 'disable', 'allow', 'prefer'])
def test_render_requires_explicit_postgres_tls(render_env, sslmode):
    url = render_env['EGASIS_DATABASE_URL'].split('?')[0]
    render_env['EGASIS_DATABASE_URL'] = url + (f'?sslmode={sslmode}' if sslmode else '')
    assert readiness(render_env, target='render')['configuration_ready'] is False


@pytest.mark.parametrize('sslmode', ['require', 'verify-ca', 'verify-full'])
def test_render_accepts_required_or_verified_tls(render_env, sslmode):
    render_env['EGASIS_DATABASE_URL'] = render_env['EGASIS_DATABASE_URL'].replace('sslmode=require', f'sslmode={sslmode}')
    assert readiness(render_env, target='render')['configuration_ready'] is True


@pytest.mark.parametrize('simulation', ['true', 'false'])
def test_render_free_rejects_worker_and_explains_smtp_block(render_env, simulation):
    render_env['EGASIS_SIMULATION'] = simulation
    report = readiness(render_env, target='render', role='worker')
    assert report['configuration_ready'] is False
    assert any(error['field'] == 'role' for error in report['errors'])
    if simulation == 'false':
        assert any('SMTP' in error['message'] for error in report['errors'])
    # A future local worker uses its own host profile, never Render Free's.
    assert readiness(render_env, target='server', role='worker')['configuration_ready'] is True


def test_render_cli_uses_render_profile(render_env, monkeypatch, capsys):
    for key, value in render_env.items():
        monkeypatch.setenv(key, value)
    assert main(['--target', 'render']) == 0
    assert json.loads(capsys.readouterr().out)['target'] == 'render'


@pytest.mark.parametrize('configured_url', [None, 'https://custom.example.com'])
@pytest.mark.parametrize('preflight_code', [0, 2])
def test_render_start_resolves_public_url_and_gates_server_start(tmp_path, configured_url, preflight_code):
    # Execute the actual folded YAML command with an inert executable. No web,
    # database or provider process is started; only shell wiring is exercised.
    blueprint = (Path(__file__).resolve().parents[1] / 'render.yaml').read_text()
    block = re.search(r'    startCommand: >-\n((?:      .+\n)+)', blueprint).group(1)
    command = ' '.join(line.strip() for line in block.splitlines())
    executable = tmp_path / 'python'
    executable.write_text('''#!/bin/sh
if [ "$2" = "egasis.deployment_check" ]; then
  printf '%s' "$EGASIS_PUBLIC_URL" > "$TEST_URL_RECORD"
  exit "$TEST_PREFLIGHT_CODE"
fi
printf '%s' "$*" > "$TEST_START_RECORD"
''')
    executable.chmod(0o700)
    env = dict(os.environ)
    env.pop('EGASIS_PUBLIC_URL', None)
    env.update({'PATH': str(tmp_path), 'RENDER_EXTERNAL_URL': 'https://assigned.onrender.com',
                'TEST_URL_RECORD': str(tmp_path / 'url'), 'TEST_START_RECORD': str(tmp_path / 'start'),
                'TEST_PREFLIGHT_CODE': str(preflight_code), 'PORT': '12000'})
    if configured_url:
        env['EGASIS_PUBLIC_URL'] = configured_url
    result = subprocess.run(['/bin/sh', '-c', command], env=env, capture_output=True, text=True)
    assert result.returncode == preflight_code
    assert (tmp_path / 'url').read_text() == (configured_url or 'https://assigned.onrender.com')
    assert (tmp_path / 'start').exists() is (preflight_code == 0)
    if preflight_code == 0:
        assert (tmp_path / 'start').read_text() == '-m uvicorn egasis.app:app --host 0.0.0.0 --port 12000 --workers 1'
