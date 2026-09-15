import os
import ipaddress
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url


def normalize_database_url(value):
    """Normalize hosting aliases without connecting or exposing credentials."""
    try:
        if not isinstance(value, str) or not value.strip():
            raise ValueError()
        url = make_url(value.strip())
        if url.drivername in {'postgres', 'postgresql', 'postgresql+psycopg2'}:
            if not (url.host and url.database and url.username):
                raise ValueError()
            url = url.set(drivername='postgresql+psycopg2')
        elif url.drivername not in {'sqlite', 'sqlite+pysqlite'}:
            raise ValueError()
        return url.render_as_string(hide_password=False)
    except Exception:
        raise RuntimeError('EGASIS_DATABASE_URL debe ser una URL SQLite o PostgreSQL válida con el driver disponible.') from None


def validate_public_url(value, production=False):
    try:
        if not isinstance(value, str):
            raise ValueError()
        value = value.strip().rstrip('/')
        url = urlsplit(value)
        hostname = url.hostname
        if (not hostname or url.username or url.password or url.query or url.fragment or url.path
                or url.scheme not in {'http', 'https'} or not re.fullmatch(r'[a-zA-Z0-9.:-]+', hostname)):
            raise ValueError()
        port = url.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        local = hostname.lower() == 'localhost'
        try:
            local = local or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
        if production and (url.scheme != 'https' or local):
            raise ValueError()
        return value
    except (TypeError, ValueError):
        raise RuntimeError('EGASIS_PUBLIC_URL debe ser un origen válido sin credenciales, rutas ni parámetros; HTTPS público en producción.') from None


def validate_secret(value, name):
    if (not isinstance(value, str) or value != value.strip() or len(value) < 32
            or len(set(value)) < 8):
        raise RuntimeError(f'{name} debe contener al menos 32 caracteres variados, sin espacios iniciales o finales.')
    return value


def parse_simulation(value):
    if not isinstance(value, str) or value.strip().lower() not in {'true', 'false'}:
        raise RuntimeError('EGASIS_SIMULATION debe ser true o false.')
    return value.strip().lower() == 'true'

@dataclass
class Settings:
    database_url: str = field(repr=False)
    secret_key: str = field(repr=False)
    public_url: str = 'http://127.0.0.1:8765'
    simulation: bool = True
    production: bool = False
    registration_token: str = field(default='', repr=False)

    @classmethod
    def from_env(cls, env=None, *, data_dir=None, initialize_local=True):
        env = os.environ if env is None else env
        environment = env.get('EGASIS_ENV', 'development')
        if environment not in {'development', 'test', 'production'}:
            raise RuntimeError('EGASIS_ENV debe ser development, test o production.')
        production = environment == 'production'
        root = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parents[1] / 'data'
        raw_database = env.get('EGASIS_DATABASE_URL') or env.get('DATABASE_URL')
        if production and not raw_database:
            raise RuntimeError('EGASIS_DATABASE_URL (o DATABASE_URL) es obligatorio en producción.')
        database_url = normalize_database_url(raw_database or f'sqlite:///{root / "egasis.db"}')
        database = make_url(database_url)
        if production and database.get_backend_name() == 'sqlite':
            if not database.database or not Path(database.database).is_absolute():
                raise RuntimeError('SQLite en producción requiere una ruta absoluta en disco persistente.')
        key = env.get('EGASIS_SECRET_KEY', '')
        if not key and production:
            raise RuntimeError('EGASIS_SECRET_KEY is required in production')
        if production:
            validate_secret(key, 'EGASIS_SECRET_KEY')
        public_url = validate_public_url(env.get('EGASIS_PUBLIC_URL', 'http://127.0.0.1:8765'), production)
        simulation = parse_simulation(env.get('EGASIS_SIMULATION', 'true'))
        registration_token = env.get('EGASIS_REGISTRATION_TOKEN', '')
        if production and registration_token:
            validate_secret(registration_token, 'EGASIS_REGISTRATION_TOKEN')
        # Production with PostgreSQL never creates or reads local state. This
        # also supports function runtimes where application files are read-only.
        if not production and initialize_local and (not raw_database or not key):
            root.mkdir(parents=True, exist_ok=True)
        if not key:
            key_path = root / '.development-key'
            if not key_path.exists():
                if not initialize_local:
                    raise RuntimeError('EGASIS_SECRET_KEY debe configurarse para esta comprobación sin escritura local.')
                try:
                    fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(fd, 'w') as f:
                        f.write(secrets.token_urlsafe(48))
                except FileExistsError:
                    pass
            key = key_path.read_text().strip()
        return cls(database_url, key, public_url, simulation, production, registration_token)
