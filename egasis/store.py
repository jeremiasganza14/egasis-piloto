from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from .schema import ensure_schema

def make_store(url):
    options = {'connect_args': {'check_same_thread': False, 'timeout': 30}} if url.startswith('sqlite') else {}
    if not url.startswith('sqlite'):
        # Free managed databases may suspend idle connections. Check pooled
        # connections before handing them to the first request after wake-up.
        options.update(pool_pre_ping=True, pool_recycle=300, connect_args={'connect_timeout': 10})
    engine = create_engine(url, **options)
    if url.startswith('sqlite'):
        @event.listens_for(engine, 'connect')
        def configure(conn, _):
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA busy_timeout=30000')
            conn.execute('PRAGMA journal_mode=WAL')
    ensure_schema(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)
