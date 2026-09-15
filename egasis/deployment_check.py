"""Offline deployment configuration check. Never opens a database or network.

This validates configuration only. It cannot certify credentials, DNS, provider
plans, schema state, delivery or a running worker. Output contains no env values.
"""
import argparse
import importlib.util
import ipaddress
import json
import os

from sqlalchemy.engine import make_url

from .settings import normalize_database_url, parse_simulation, validate_public_url, validate_secret


def readiness(env=None, *, target='railway', role='web', railway_plan=None, module_available=None):
    env = os.environ if env is None else env
    available = module_available or (lambda module: importlib.util.find_spec(module) is not None)
    errors, notes = [], []

    def error(field, message):
        errors.append({'field': field, 'message': message})

    if target not in {'railway', 'vercel', 'render', 'server'} or role not in {'web', 'worker'}:
        error('target', 'Destino o proceso desconocido.')
    if env.get('EGASIS_ENV') != 'production':
        error('EGASIS_ENV', 'Usá production para habilitar las validaciones y cookies de producción.')
    try:
        validate_secret(env.get('EGASIS_SECRET_KEY', ''), 'EGASIS_SECRET_KEY')
    except RuntimeError as exc:
        error('EGASIS_SECRET_KEY', str(exc))
    try:
        validate_public_url(env.get('EGASIS_PUBLIC_URL', ''), production=True)
    except RuntimeError as exc:
        error('EGASIS_PUBLIC_URL', str(exc))
    registration = env.get('EGASIS_REGISTRATION_TOKEN', '')
    if registration:
        try:
            validate_secret(registration, 'EGASIS_REGISTRATION_TOKEN')
        except RuntimeError as exc:
            error('EGASIS_REGISTRATION_TOKEN', str(exc))
    else:
        notes.append('El registro de nuevos espacios quedará cerrado; las cuentas existentes pueden ingresar.')
    simulation = None
    try:
        simulation = parse_simulation(env.get('EGASIS_SIMULATION', 'true'))
    except RuntimeError as exc:
        error('EGASIS_SIMULATION', str(exc))
    backend = None
    raw_database = env.get('EGASIS_DATABASE_URL') or env.get('DATABASE_URL')
    if not raw_database:
        error('EGASIS_DATABASE_URL', 'Configurá EGASIS_DATABASE_URL o DATABASE_URL; no se crea una base local como alternativa.')
    else:
        try:
            url = make_url(normalize_database_url(raw_database))
            backend = url.get_backend_name()
            if target in {'railway', 'vercel', 'render'} and backend != 'postgresql':
                error('EGASIS_DATABASE_URL', 'Web y worker necesitan PostgreSQL compartido para este destino.')
            if backend == 'postgresql':
                if not available('psycopg2'):
                    error('psycopg2', 'Instalá las dependencias de requirements.txt para habilitar PostgreSQL.')
                if target in {'vercel', 'render'}:
                    host = (url.host or '').lower()
                    private = (host == 'localhost' or host.endswith(('.internal', '.local', '.localhost'))
                               or '.' not in host)
                    try:
                        private = private or not ipaddress.ip_address(host).is_global
                    except ValueError:
                        pass
                    if private:
                        error('EGASIS_DATABASE_URL', 'Este destino necesita PostgreSQL externo con dirección pública; no sirve una dirección local o privada.')
                    if url.query.get('sslmode') not in {'require', 'verify-ca', 'verify-full'}:
                        error('EGASIS_DATABASE_URL', 'La conexión PostgreSQL externa requiere sslmode=require o una verificación TLS más estricta.')
            elif backend == 'sqlite':
                from pathlib import Path
                if not url.database or not Path(url.database).is_absolute():
                    error('EGASIS_DATABASE_URL', 'SQLite de producción requiere una ruta absoluta y disco persistente.')
        except RuntimeError as exc:
            error('EGASIS_DATABASE_URL', str(exc))
    if target == 'railway' and role == 'worker' and simulation is False:
        if railway_plan not in {'pro', 'enterprise'}:
            error('railway_plan', 'El worker SMTP requiere Railway Pro o superior. Declarar el plan no lo compra ni verifica con Railway.')
        else:
            notes.append('El plan SMTP se tomó de la declaración del operador; no se verificó con Railway.')
    if target == 'vercel':
        if role == 'worker':
            error('role', 'El worker continuo debe alojarse fuera de las funciones de Vercel.')
        notes.append('La web en Vercel requiere worker externo, PostgreSQL compartido y operaciones dentro del límite de cada función.')
        notes.append('El uso comercial en Vercel requiere Pro o Enterprise; el comprobador no verifica el plan contratado.')
    if target == 'render':
        if role == 'worker':
            error('role', 'Este perfil Render Free admite solo la web; el worker debe ejecutarse fuera de Render Free.')
            if simulation is False:
                error('EGASIS_SIMULATION', 'Render Free bloquea los puertos SMTP 25, 465 y 587; no puede enviar el correo del worker actual.')
        notes.append('Render Free se suspende por inactividad y pierde archivos locales; los datos deben permanecer en PostgreSQL externo.')
        notes.append('La plantilla gratuita mantiene simulación activa y no inicia un worker. Los ciclos simulados se procesan desde la interfaz.')
        notes.append('El perfil declara Render Free; no verifica el plan, la cuenta ni los límites de Render o de la base externa.')
    if simulation is True:
        notes.append('La simulación permanece activa; esta configuración no enviará correo real.')
    notes.append('No se verificaron red, credenciales, migraciones, procesos en ejecución ni servicios externos.')
    return {'configuration_ready': not errors, 'scope': 'offline_configuration_only',
            'target': target if target in {'railway', 'vercel', 'render', 'server'} else 'unknown',
            'role': role if role in {'web', 'worker'} else 'unknown', 'database_backend': backend,
            'simulation': simulation, 'registration': 'invitation' if registration else 'closed',
            'errors': errors, 'notes': notes}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Comprueba configuración sin abrir bases, redes ni archivos de estado.')
    parser.add_argument('--target', choices=['railway', 'vercel', 'render', 'server'], default='railway')
    parser.add_argument('--role', choices=['web', 'worker'], default='web')
    parser.add_argument('--railway-plan', choices=['free', 'trial', 'hobby', 'pro', 'enterprise'],
                        help='Declaración del operador; no consulta ni modifica el plan.')
    args = parser.parse_args(argv)
    result = readiness(target=args.target, role=args.role, railway_plan=args.railway_plan)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['configuration_ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
