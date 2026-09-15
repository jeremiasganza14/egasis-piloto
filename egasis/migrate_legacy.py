"""Read-only legacy SQLite importer; dry-run unless --apply is explicit."""
import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from .models import Campaign, Contact, Event, Message, Suppression, Workspace


class MigrationError(ValueError):
    pass


_EMAIL = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
_LEAD_COLUMNS = ('id', 'campaign_id', 'email', 'name', 'company', 'website', 'status', 'fit_reason')
_MESSAGE_COLUMNS = ('id', 'lead_id', 'subject', 'body', 'from_email', 'classification',
                    'bounced', 'spam_complaint', 'unsubscribed', 'status')


def _source_path(source):
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise MigrationError('La base de origen no existe o no es un archivo.')
    return path


def _check_target(source, url):
    parsed = make_url(url)
    if parsed.get_backend_name() == 'sqlite' and parsed.database and parsed.database != ':memory:':
        target = Path(parsed.database).expanduser().resolve()
        if source == target or (target.exists() and source.samefile(target)):
            raise MigrationError('Origen y destino deben ser bases diferentes.')


def _rows(db, table, allowed):
    # Names are fixed code-owned allowlists. Do not read settings, account or API
    # key tables: legacy databases may contain unencrypted credentials.
    cols = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
    selected = [column for column in allowed if column in cols]
    if not selected:
        return []
    return [dict(row) for row in db.execute(
        'SELECT ' + ', '.join('"' + column + '"' for column in selected) + f' FROM "{table}"')]


def import_legacy(source, factory, target_workspace, apply=False, source_client=None):
    """Import supported Leadgen Studio/Nexus records into an existing workspace.

    Historical records are visibly imported, never queued or claimed as newly
    delivered. A repeat from the same canonical source path is idempotent.
    """
    path = _source_path(source)
    with factory() as target:
        _check_target(path, target.bind.url)
        if target.bind.dialect.name == 'sqlite':
            target.execute(text('BEGIN IMMEDIATE' if apply else 'BEGIN'))
        try:
            workspace = target.scalar(select(Workspace).where(Workspace.id == target_workspace).with_for_update())
            if not workspace:
                raise MigrationError('El espacio de destino no existe; crealo desde Egasis antes de importar.')
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as legacy:
                legacy.row_factory = sqlite3.Row
                legacy.execute('PRAGMA query_only=ON')
                legacy.execute('BEGIN')
                tables = {row[0] for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'leads' not in tables:
                    raise MigrationError('Origen no reconocido: falta la tabla leads.')
                leads = _rows(legacy, 'leads', _LEAD_COLUMNS)
                campaigns = _rows(legacy, 'campaigns', ('id', 'client_id', 'name')) if 'campaigns' in tables else []
                client_ids = {c.get('client_id') for c in campaigns if c.get('client_id') is not None}
                if len(client_ids) > 1 and source_client is None:
                    raise MigrationError('El origen tiene varios clientes. Indicá --source-client para conservar su separación.')
                if source_client is not None:
                    if source_client not in client_ids:
                        raise MigrationError('El cliente solicitado no tiene campañas en el origen.')
                    campaigns = [c for c in campaigns if c.get('client_id') == source_client]
                    selected_ids = {c['id'] for c in campaigns}
                    leads = [lead for lead in leads if lead.get('campaign_id') in selected_ids]
                originals = {c['id']: c for c in campaigns}
                settings = {c['campaign_id']: c for c in _rows(legacy, 'campaign_settings',
                            ('campaign_id', 'subject_1', 'body_1'))} if 'campaign_settings' in tables else {}
                outbound_table = 'sent_emails' if 'sent_emails' in tables else 'email_logs'
                outbound = _rows(legacy, outbound_table, _MESSAGE_COLUMNS) if outbound_table in tables else []
                inbound = _rows(legacy, 'replies', _MESSAGE_COLUMNS) if 'replies' in tables else []

            fingerprint = hashlib.sha256(str(path).encode()).hexdigest()[:20]
            report = {'dry_run': not apply, 'workspace_id': target_workspace, 'campaigns': 0,
                      'contacts': 0, 'messages': 0, 'suppressions': 0, 'duplicates': 0,
                      'invalid_contacts': 0, 'credentials_imported': 0, 'meetings_imported': 0}
            campaign_map, contact_map = {}, {}
            campaign_cache = {c.name: c for c in target.scalars(select(Campaign).where(Campaign.workspace_id == target_workspace))}
            contact_cache = {(c.campaign_id, c.email): c for c in target.scalars(select(Contact).where(Contact.workspace_id == target_workspace))}
            suppression_cache = set(target.scalars(select(Suppression.email).where(Suppression.workspace_id == target_workspace)))
            message_keys = set(target.scalars(select(Message.idempotency_key).where(Message.workspace_id == target_workspace)))

            def suppress(email, reason):
                if email not in suppression_cache:
                    target.add(Suppression(workspace_id=target_workspace, email=email, reason=reason))
                    suppression_cache.add(email)
                    report['suppressions'] += 1

            for lead in leads:
                email = str(lead.get('email') or '').strip().lower()
                if len(email) > 254 or not _EMAIL.fullmatch(email):
                    report['invalid_contacts'] += 1
                    continue
                old_campaign = lead.get('campaign_id') or 0
                if old_campaign not in campaign_map:
                    title = str(originals.get(old_campaign, {}).get('name') or 'Historial anterior')[:100]
                    name = f'Importación {fingerprint}/{old_campaign} · {title}'[:160]
                    campaign = campaign_cache.get(name)
                    if not campaign:
                        template = settings.get(old_campaign, {})
                        campaign = Campaign(workspace_id=target_workspace, name=name, status='draft',
                                            subject=template.get('subject_1') or 'Revisar asunto antes de activar',
                                            body=template.get('body_1') or 'Revisar la oferta y redactar el primer mensaje antes de activar.',
                                            reply_mode='review')
                        target.add(campaign)
                        target.flush()
                        campaign_cache[name] = campaign
                        report['campaigns'] += 1
                    campaign_map[old_campaign] = campaign
                campaign = campaign_map[old_campaign]
                contact = contact_cache.get((campaign.id, email))
                if not contact:
                    contact = Contact(workspace_id=target_workspace, campaign_id=campaign.id, email=email,
                                      name=str(lead.get('name') or '')[:160], company=str(lead.get('company') or '')[:200],
                                      website=str(lead.get('website') or ''), source='legacy_import',
                                      evidence=f'Importación {fingerprint}; estado anterior: {lead.get("status") or "desconocido"}',
                                      status='imported')
                    target.add(contact)
                    target.flush()
                    contact_cache[(campaign.id, email)] = contact
                    report['contacts'] += 1
                else:
                    report['duplicates'] += 1
                contact_map[lead.get('id')] = contact
                if str(lead.get('status') or '').lower() in {'do_not_contact', 'unsubscribed', 'bounced', 'suppressed', 'spam_complaint'}:
                    suppress(email, 'legacy_' + str(lead['status']).lower())

            for table, direction, rows in ((outbound_table, 'outbound', outbound), ('replies', 'inbound', inbound)):
                for row in rows:
                    contact = contact_map.get(row.get('lead_id'))
                    if not contact:
                        continue
                    # A legacy row ID distinguishes same-subject messages.
                    row_id = row.get('id')
                    if row_id is None:
                        continue
                    message_key = f'legacy:{fingerprint}:{table}:{row_id}'
                    if message_key in message_keys:
                        report['duplicates'] += 1
                        continue
                    target.add(Message(workspace_id=target_workspace, contact_id=contact.id,
                                       direction=direction, subject=str(row.get('subject') or ''),
                                       body=str(row.get('body') or ''), status='imported',
                                       classification='unclassified', idempotency_key=message_key))
                    message_keys.add(message_key)
                    report['messages'] += 1
                    if row.get('unsubscribed') or row.get('spam_complaint') or row.get('bounced') or row.get('status') == 'bounced':
                        suppress(contact.email, 'legacy_delivery_block')

            if apply:
                target.add(Event(workspace_id=target_workspace, kind='migration.completed',
                                 detail=json.dumps(dict(report, source_fingerprint=fingerprint), sort_keys=True)))
                target.commit()
            else:
                target.rollback()
            return report
        except Exception:
            target.rollback()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Importa una base anterior en borradores. Sin --apply solo informa cambios.')
    parser.add_argument('--source', required=True, help='Archivo SQLite anterior; se abre en modo de solo lectura.')
    parser.add_argument('--target-workspace', required=True, type=int)
    parser.add_argument('--database-url', required=True, help='URL de la nueva base de Egasis; debe existir el espacio de destino.')
    parser.add_argument('--source-client', type=int, help='Cliente Nexus; obligatorio si el origen contiene varios clientes.')
    parser.add_argument('--apply', action='store_true', help='Confirma la escritura al destino; nunca escribe al origen.')
    args = parser.parse_args(argv)
    engine = None
    try:
        path = _source_path(args.source)
        _check_target(path, args.database_url)
        url = make_url(args.database_url)
        if url.get_backend_name() == 'sqlite' and (not url.database or not Path(url.database).expanduser().is_file()):
            raise MigrationError('La base de destino debe existir. Iniciá Egasis y registrá el espacio primero.')
        engine = create_engine(args.database_url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        result = import_legacy(path, factory, args.target_workspace, args.apply, args.source_client)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MigrationError, sqlite3.DatabaseError) as exc:
        parser.exit(2, f'No se importó: {exc}\n')
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
