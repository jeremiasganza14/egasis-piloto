"""Portable, deterministic meeting preparation from workspace-owned records.

No generation service is called. The editable five-slide template was authored
with @oai/artifact-tool. Export only replaces its text nodes using stdlib OOXML;
no presentation runtime, credentials, external assets, or network are required.
Long excerpts are visibly shortened on slides and retained in the speaker notes.
"""
import base64
from datetime import datetime, timezone
from html import escape
from io import BytesIO
import json
import math
import re
import time
import unicodedata
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select

from .models import Campaign, Contact, Meeting, Message, Suppression, Workspace


QUESTIONS = [
    '¿Qué necesidad quieren resolver primero?',
    '¿Cómo trabajan hoy y qué necesitan cambiar?',
    '¿Qué alcance, plazo y presupuesto quieren evaluar?',
    '¿Quiénes participarían en la decisión?',
]
_XML_TEXT = '{http://schemas.openxmlformats.org/drawingml/2006/main}t'


def _plain(value):
    value = str(value or '')
    # XML 1.0 excludes isolated surrogates and U+FFFE/U+FFFF even though JSON can
    # carry them. Strip those values before they can corrupt an exported package.
    return ''.join(char for char in value if char in '\n\t\r' or
                   0x20 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD or
                   0x10000 <= ord(char) <= 0x10FFFF)


def _excerpt(value, limit):
    value = ' '.join(_plain(value).split())
    if len(value) <= limit:
        return value
    return value[:limit - 1].rsplit(' ', 1)[0] + '…'


def _fit(value, font_size, max_lines, width=1040, bold=False):
    """Wrap and visibly shorten excerpts to a conservative native text box size.

    Character counts alone do not bound rendered width (for example, a long
    company name consisting of W). Conservative glyph widths also handle words
    without spaces. Full source content stays in notes and the Markdown brief.
    """
    def glyph(char):
        if unicodedata.combining(char):
            return 0
        if unicodedata.east_asian_width(char) in {'W', 'F'}:
            return 1.15
        if char == ' ':
            return .34
        if char in 'iljI.,:;!|\'`':
            return .34
        if char in 'WMwm@%':
            return 1.05
        if char.isupper():
            return .76
        return .66
    factor = font_size * (1.08 if bold else 1.0)
    measure = lambda value: sum(glyph(char) for char in value) * factor
    lines, current = [], ''
    for word in ' '.join(_plain(value).split()).split(' '):
        combined = current + (' ' if current else '') + word
        if current and measure(combined) > width:
            lines.append(current)
            current = ''
        while measure(word) > width:
            part = ''
            for char in word:
                if measure(part + char) > width:
                    break
                part += char
            lines.append(part)
            word = word[len(part):]
        current += (' ' if current else '') + word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and measure(lines[-1] + '…') > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + '…'
    return '\n'.join(lines)


def _date(value, zone='UTC'):
    try:
        local = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, TypeError):
        local = timezone.utc
    try:
        if not math.isfinite(float(value)):
            raise ValueError
        return datetime.fromtimestamp(value, local).strftime('%d/%m/%Y %H:%M') + ' (' + str(local) + ')'
    except (ValueError, TypeError, OSError, OverflowError):
        return 'Fecha sin registrar'


def _website_url(value):
    try:
        parsed = urlsplit(value)
        return bool(parsed.scheme in {'https', 'http'} and parsed.hostname and parsed.username is None
                    and parsed.password is None and not re.search(r'[\x00-\x20\\]', value))
    except (ValueError, TypeError):
        return False


def build_brief(factory, wid, contact_id):
    """Return plain JSON-ready facts. Every related record is workspace-scoped."""
    with factory() as db:
        workspace = db.get(Workspace, wid)
        contact = db.scalar(select(Contact).where(Contact.id == contact_id, Contact.workspace_id == wid))
        if not workspace or not contact:
            raise ValueError('El contacto no está disponible en este espacio.')
        campaign = db.scalar(select(Campaign).where(Campaign.id == contact.campaign_id, Campaign.workspace_id == wid))
        if not campaign:
            raise ValueError('La campaña no está disponible en este espacio.')
        history_query = select(Message).where(Message.workspace_id == wid, Message.contact_id == contact_id,
            or_(Message.direction == 'inbound', (Message.direction == 'outbound') & Message.status.in_(['sent', 'simulated'])))
        history_count = db.scalar(select(func.count()).select_from(history_query.subquery()))
        history = list(reversed(db.scalars(history_query.order_by(Message.created_at.desc(), Message.id.desc()).limit(50)).all()))
        meetings = db.scalars(select(Meeting).where(Meeting.workspace_id == wid, Meeting.contact_id == contact_id)
                             .order_by(Meeting.starts_at, Meeting.id)).all()
        suppressed = bool(db.scalar(select(Suppression.id).where(Suppression.workspace_id == wid,
            func.lower(Suppression.email) == contact.email.lower()))) or contact.status in {
                'unsubscribed', 'suppressed', 'do_not_contact', 'bounced', 'not_interested'}
        evidence = []
        try:
            raw = json.loads(contact.evidence or '{}')
            if isinstance(raw, dict) and isinstance(raw.get('text'), str) and raw['text'].strip() and _website_url(raw.get('url')):
                evidence.append({'text': _plain(raw['text'])[:16000], 'url': raw['url'],
                                 'retrieved_at': raw.get('retrieved_at'),
                                 'attribution': 'Texto publicado por el sitio. Sin verificación independiente.'})
        except (ValueError, TypeError):
            pass
        brief = {
            'schema_version': 1, 'generated_at': time.time(),
            'workspace': {'id': wid, 'name': _plain(workspace.name), 'signature': _plain(workspace.signature),
                          'timezone': workspace.timezone or 'UTC'},
            'campaign': {'id': campaign.id, 'name': _plain(campaign.name),
                         'offer': _plain(campaign.offer or workspace.offer),
                         'audience': _plain(campaign.audience or workspace.audience),
                         'offer_source': 'campaign' if campaign.offer else 'workspace'},
            'contact': {'id': contact.id, 'name': _plain(contact.name), 'company': _plain(contact.company),
                        'email': _plain(contact.email), 'website': _plain(contact.website),
                        'status': contact.status, 'source': _plain(contact.source), 'suppressed': suppressed},
            'evidence': evidence,
            'messages': [{'id': item.id, 'direction': item.direction, 'status': item.status,
                          'subject': _plain(item.subject), 'body': _plain(item.body)[:8000],
                          'body_truncated': len(item.body or '') > 8000,
                          'created_at': item.created_at, 'sent_at': item.sent_at} for item in history],
            'history_truncated': history_count > len(history),
            'meetings': [{'id': item.id, 'status': item.status, 'starts_at': item.starts_at,
                          'duration_minutes': item.duration_minutes, 'location': _plain(item.location),
                          'notes': _plain(item.notes)} for item in meetings],
            'questions': QUESTIONS.copy(),
        }
    upcoming = [item for item in brief['meetings'] if item['status'] in {'confirmed', 'proposed'}
                and isinstance(item['starts_at'], (int, float)) and item['starts_at'] >= brief['generated_at']]
    confirmed = next((item for item in upcoming if item['status'] == 'confirmed'), None)
    chosen = confirmed or (upcoming[0] if upcoming else None)
    if suppressed:
        brief['next_step'] = {'status': 'suppressed', 'title': 'Contacto excluido de nuevos mensajes',
            'detail': 'Revisar el historial internamente y respetar la exclusión registrada.',
            'note': 'Este material no autoriza nuevos envíos.'}
    elif chosen:
        is_confirmed = chosen['status'] == 'confirmed'
        brief['next_step'] = {'status': chosen['status'],
            'title': 'Reunión confirmada en Egasis' if is_confirmed else 'Horario propuesto, pendiente de confirmar',
            'detail': _date(chosen['starts_at'], brief['workspace']['timezone']) +
                      f"\nDuración registrada: {chosen['duration_minutes']} minutos" +
                      ('\nLugar o enlace: ' + chosen['location'] if chosen['location'] else ''),
            'note': 'Fuente: registro de reunión en Egasis.' if is_confirmed else 'Confirmar el horario con el contacto antes de agendar.',
            'meeting_id': chosen['id']}
    else:
        brief['next_step'] = {'status': 'unconfirmed', 'title': 'Próxima conversación por coordinar',
            'detail': 'Acordar con el contacto un objetivo y un horario para conversar.',
            'note': 'Egasis no registra una reunión futura confirmada.'}
    return brief


def _validated(brief):
    if not isinstance(brief, dict) or brief.get('schema_version') != 1 or any(
            key not in brief for key in ('workspace', 'campaign', 'contact', 'evidence', 'messages', 'meetings', 'questions', 'next_step')):
        raise ValueError('Generá primero la ficha del contacto con build_brief.')
    return brief


def _md(value):
    """Quote source text as inert Markdown, including HTML and embedded images."""
    value = escape(_plain(value), quote=False)
    return re.sub(r'([\\`*_{}\[\]()#+.!|>])', r'\\\1', value)


def _quoted(value):
    return '\n'.join('> ' + _md(line) for line in _plain(value).splitlines())


def export_markdown(brief):
    brief = _validated(brief)
    contact, campaign, workspace = brief['contact'], brief['campaign'], brief['workspace']
    lines = ['# Preparación de reunión', '',
             '**Contacto:** ' + _md(contact['name'] or contact['email']),
             '**Empresa:** ' + _md(contact['company'] or 'Sin registrar'),
             '**Espacio:** ' + _md(workspace['name']),
             '**Fecha de preparación:** ' + _md(_date(brief['generated_at'], workspace['timezone'])),
             '', '## Contexto', '']
    if brief['evidence']:
        for item in brief['evidence']:
            lines += [_quoted(item['text']), '', 'Fuente: ' + _md(item['url']),
                      _md(item['attribution']), 'Fecha de lectura: ' + _md(_date(item['retrieved_at'], workspace['timezone'])), '']
    else:
        lines += ['Sin investigación atribuida del sitio. Los datos del contacto provienen de su registro en Egasis.', '']
    lines += ['## Nuestra oferta', '', _quoted(campaign['offer'] or 'Oferta pendiente de configurar.'), '',
              '**Público definido:** ' + _md(campaign['audience'] or 'Pendiente de configurar.'),
              'Fuente: configuración de la campaña o del espacio.', '', '## Conversación registrada', '']
    if not brief['messages']:
        lines += ['Sin mensajes recibidos o enviados registrados.', '']
    for item in brief['messages']:
        label = 'Mensaje recibido' if item['direction'] == 'inbound' else 'Salida simulada' if item['status'] == 'simulated' else 'Mensaje enviado'
        lines += ['### ' + label + ' · ' + _md(_date(item['created_at'], workspace['timezone'])),
                  '', '**Asunto:** ' + _md(item['subject']), '', _quoted(item['body']), '']
        if item['body_truncated']:
            lines += ['Extracto limitado a 8.000 caracteres. El historial de Egasis conserva el mensaje.', '']
    if brief['history_truncated']:
        lines += ['Esta ficha incluye los últimos 50 mensajes registrados.', '']
    lines += ['## Preguntas sugeridas', ''] + [f'{index}. {_md(question)}' for index, question in enumerate(brief['questions'], 1)]
    next_step = brief['next_step']
    lines += ['', '## Próximo paso', '', _md(next_step['title']), '', _md(next_step['detail']), '', _md(next_step['note']), '',
              '## Reuniones registradas', '']
    if not brief['meetings']:
        lines.append('Sin registros de reunión.')
    for item in brief['meetings']:
        labels = {'proposed': 'Propuesta', 'confirmed': 'Confirmada en Egasis', 'cancelled': 'Cancelada', 'completed': 'Finalizada'}
        lines += ['- ' + _md(labels.get(item['status'], item['status'])) + ': ' + _md(_date(item['starts_at'], workspace['timezone']))]
        if item['notes']:
            lines += ['', _quoted(item['notes']), '']
    return BytesIO(('\n'.join(lines) + '\n').encode('utf-8'))


def _slide_text(brief):
    contact, campaign, workspace = brief['contact'], brief['campaign'], brief['workspace']
    evidence = brief['evidence'][0] if brief['evidence'] else None
    incoming = next((item for item in reversed(brief['messages']) if item['direction'] == 'inbound'), None)
    outgoing = next((item for item in reversed(brief['messages']) if item['direction'] == 'outbound'), None)
    zone = workspace['timezone']
    mapping = {
        'TITLE1': _fit(contact['company'] or contact['name'] or 'Preparación de reunión', 60, 2, bold=True),
        'CONTACT1': _fit(contact['name'] or 'Contacto sin nombre', 28, 1) + '\n' + _fit(contact['email'], 28, 1),
        'EVIDENCE1': _fit(evidence['text'], 26, 3) if evidence else 'Sin investigación atribuida del sitio. Los datos disponibles provienen del registro del contacto.',
        'SOURCE1': _fit('Fuente: ' + evidence['url'], 15, 1) if evidence else 'Fuente: contacto registrado en Egasis',
        'WORKSPACE2': _fit(workspace['name'], 22, 1),
        'OFFER2': _fit(campaign['offer'] or 'Oferta pendiente de configurar.', 32, 5),
        'AUDIENCE2': _fit(campaign['audience'] or 'Público pendiente de configurar.', 25, 2),
        'INBOUND_LABEL3': _fit('ÚLTIMO MENSAJE RECIBIDO' + (' · ' + _date(incoming['created_at'], zone) if incoming else ''), 18, 1),
        'INBOUND3': _fit(incoming['body'], 27, 4) if incoming else 'Todavía no hay una respuesta registrada del contacto.',
        'OUTBOUND_LABEL3': _fit(('ÚLTIMA SALIDA SIMULADA' if outgoing and outgoing['status'] == 'simulated' else 'ÚLTIMO MENSAJE ENVIADO') + (' · ' + _date(outgoing['created_at'], zone) if outgoing else ''), 18, 1),
        'OUTBOUND3': _fit(outgoing['body'], 27, 3) if outgoing else 'Todavía no hay un mensaje enviado registrado.',
        'QUESTIONS4': '\n\n'.join(f'{index}. {question}' for index, question in enumerate(brief['questions'], 1)),
        'NEXT_TITLE5': _fit(brief['next_step']['title'], 36, 2, bold=True),
        'NEXT_DETAIL5': _fit(brief['next_step']['detail'], 27, 4),
        'NEXT_NOTE5': _fit(brief['next_step']['note'], 22, 2),
    }
    source_note = 'Contacto registrado en Egasis.\n' + json.dumps(contact, ensure_ascii=False)
    if evidence:
        source_note += '\n\n' + evidence['attribution'] + '\nFuente: ' + evidence['url'] + '\n' + evidence['text']
    mapping['NOTES1'] = source_note
    mapping['NOTES2'] = 'Fuente: oferta y público configurados por el propietario.\n' + campaign['offer'] + '\n\nPúblico:\n' + campaign['audience']
    mapping['NOTES3'] = 'Extractos de la conversación registrada. Las salidas simuladas se identifican como tales.\n\n' + '\n\n'.join(
        item['direction'] + ' (' + item['status'] + ') ' + _date(item['created_at'], zone) + '\nAsunto: ' + item['subject'] + '\n' + item['body']
        for item in brief['messages'])
    mapping['NOTES4'] = 'Preguntas sugeridas para la reunión. No describen hechos o acuerdos confirmados.'
    mapping['NOTES5'] = 'El estado procede de registros de reunión de Egasis, nunca de inferir una reserva a partir de correos o enlaces.\n' + json.dumps(brief['next_step'], ensure_ascii=False)
    return mapping


def export_pptx(brief):
    """Return an editable five-slide PPTX stream positioned at byte zero."""
    mapping = _slide_text(_validated(brief))
    source = BytesIO(base64.b64decode(_TEMPLATE_B64))
    result = BytesIO()
    with ZipFile(source) as template, ZipFile(result, 'w', ZIP_DEFLATED) as output:
        for member in template.infolist():
            data = template.read(member.filename)
            if re.fullmatch(r'ppt/(?:slides/slide\d+|notesSlides/notesSlide\d+)\.xml', member.filename):
                root = ET.fromstring(data)
                for node in root.iter(_XML_TEXT):
                    value = node.text or ''
                    match = re.fullmatch(r'\{\{([A-Z0-9_]+)\}\}', value)
                    if match:
                        node.text = _plain(mapping[match.group(1)])
                data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            output.writestr(member, data)
    result.seek(0)
    return result


# Editable template compiled with the bundled presentation authoring tool.
_TEMPLATE_B64 = (
    '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'
    'rYssXao0y7Z64s/oWVbx9zS/faUintbV7U2QVeA+zlxL2UoqZoHJ2enkhZ6z00mvvRVaN8s/Kq2NlN+fV+V1af4tp8n8flGBdOla'
    '2AJ5vNau1XbX/rNt1/3OTUOj6FUPt70YTzerar2vj1sVu16N5Z36tybLnclGu940F3UzRIz1pgF3Vepaj0oRn0eKQaUUgQz5DPoR'
    'c6AiVEbEVgGh4sncg8U0qXSbFbNla9yjxD5VkkXQIQJD5hEGJScScuJJqkLb84PwyertxeKNxes0qYq6WDUnSbGeFKtVmuje5glB'
    'mL2IWGt0f76ojbiN1DZk/RjV5UWR/FWDvDivynbUhxmwnYpXmfI8isPliOmnWK3AxrVsQRBCFnhwLSpxK076IUw2roWRELK9nJgm'
    'lFKb9xEzppi2ZVU357pYAyO4VqWTZu+omH7je5OPna5eh7meF0MUg87PLAffXQsNYu/Wrs7gLG8HsBu2ctps/GL5YFrdFMuHRTWM'
    'Pu+uKVZpH6Ou59arurluHjI9TIzKIYzdpu9Sr64WFaj/di1MObLAjYn+T1X+TZVmSjhc/U+VjqdZ3KQ5aB5KvYoT7Vq/6exeN2kS'
    'g7m+030uxx+1SOr3W0x6w7tc7FzpDkfpTNV50JxF59717BoAMAEALK6ihXflBbMf/8xBGIGr6OvcyOaOrjZu/W8zu8/oNsnf5ww5'
    'aM7YtgiUTyPIPV9AJjmDUgYMMixthYmUIpQjZ4bjjEMp4+9zpm89cmbkzN6cocKk0o15ht4vzdgmHuWHzZnjcmbHmfmdrpsqBsVK'
    'V028N1DoQQPF84WPbM6hgxmGzLYjKDkPYOBRJhDyAxwEI1D2BApGkku+RYogVMrtxOSZKUIIm2+JQoSwR6KMRBlm5kLQLzRzOSpn'
    'dkRBZG+KsIOmiFDKY36EoCDUTEuohL7vCCgl9xD1whB7aKTIcNMSLLFD3jDk1foXI85IkZEiw1BE8F+IIkflzI4ij49/Xl79fr3w'
    'gog8Pe1NFH7QROHYR+adGOLACSHzPAkl8jlUkQyoUtQXERuJMhxRCKOSfbCjgh1OBBm3VEakDIEU0qbbJ6rwQawOfYCU43LmBVIu'
    'lYquhsCJOGycSM/2vcDcEQrISISgT4kHpU08HihFkYNHnAyHE2Yj3u4yvoMTInm75DXSZKTJuEH/0wTlqJzZ0WTx41//YhZcgjBS'
    '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'
    '1WppnebBj6xrGDrwYAIP0hqt2CTL/jDNpaERra6UiNHjkwNI+JtbhxXOXVouN3NHpCjomBLDNRSUEtav9Pu6uYkb2WzKPiA0w5Dn'
    '7dLpnjg/hrhw/Fma5oBzn2KHGpl36T9TngyUKyUFkDvNGyBzxWtYWSXAkfFey8DR472t/3ti7K3DpPo0hHcpOqdixBUJW4SCeiXu'
    'dEOJFG1Bs+FEty0N9lJ3Hneqv9Z+PmgvU7u8Vz35GaoXVmx7zeMjNGMe2n9WbCPdeHTuTsGR58qHKmwVnAYNvwsTQVJJwuzlpXx8'
    'uqkuX1+nLM7jN7rDc0weDX4c0Q0XhzehXOsFuIOmOP8ZTeGVKNe6b4vL37bIDq37UPIUunc7+to/5bVyDxwfN6mImvsA7jr9Qmma'
    'U9VwnyOWJb4xszdQSwMEFAAAAAAAjokoXcIaJGduAgAAbgIAAAsAAABfcmVscy8ucmVsc++7vzw/eG1sIHZlcnNpb249IjEuMCIg'
    'ZW5jb2Rpbmc9InV0Zi04Ij8+PFJlbGF0aW9uc2hpcHMgeG1sbnM9Imh0dHA6Ly9zY2hlbWFzLm9wZW54bWxmb3JtYXRzLm9yZy9w'
    'YWNrYWdlLzIwMDYvcmVsYXRpb25zaGlwcyI+PFJlbGF0aW9uc2hpcCBUeXBlPSJodHRwOi8vc2NoZW1hcy5vcGVueG1sZm9ybWF0'
    'cy5vcmcvcGFja2FnZS8yMDA2L3JlbGF0aW9uc2hpcHMvbWV0YWRhdGEvY29yZS1wcm9wZXJ0aWVzIiBUYXJnZXQ9Ii9kb2NQcm9w'
    'cy9jb3JlLnhtbCIgSWQ9IlI4YjRiZjQxNzI1Y2U0N2M4IiAvPjxSZWxhdGlvbnNoaXAgVHlwZT0iaHR0cDovL3NjaGVtYXMub3Bl'
    'bnhtbGZvcm1hdHMub3JnL29mZmljZURvY3VtZW50LzIwMDYvcmVsYXRpb25zaGlwcy9leHRlbmRlZC1wcm9wZXJ0aWVzIiBUYXJn'
    'ZXQ9Ii9kb2NQcm9wcy9hcHAueG1sIiBJZD0iUjBiNTE2MjA5MDY2YjQ1YjQiIC8+PFJlbGF0aW9uc2hpcCBUeXBlPSJodHRwOi8v'
    'c2NoZW1hcy5vcGVueG1sZm9ybWF0cy5vcmcvb2ZmaWNlRG9jdW1lbnQvMjAwNi9yZWxhdGlvbnNoaXBzL29mZmljZURvY3VtZW50'
    'IiBUYXJnZXQ9Ii9wcHQvcHJlc2VudGF0aW9uLnhtbCIgSWQ9IlI1YzI3ODc0NDJlZmY0MDMxIiAvPjwvUmVsYXRpb25zaGlwcz5Q'
    'SwMEFAAAAAgAjokoXd5PthnEAAAAMQEAACwAAABwcHQvbm90ZXNNYXN0ZXJzL19yZWxzL25vdGVzTWFzdGVyMS54bWwucmVsc43P'
    'PU7DQBCG4auspsfjxMH8yOs0aShoolxgWc/aK/ZPO5PInI2CI3EFCiiIREHzVa8e6ft8/xj2awzqQpV9Tho2TQuKks2TT7OGs7ib'
    'e9iPw5GCEZ8TL76wWmNIrGERKY+IbBeKhptcKK0xuFyjEW5ynbEY+2pmwm3b9lh/G3BtqtNbof+I2Tlv6ZDtOVKSP2CUhSKBOpk6'
    'k2jAUgRTFuJnw0L1J/jerlljAPU0aTh2/e3mZervtjtyu4fOgsJxwKvj4xdQSwMEFAAAAAgAjokoXWjU0SDcAAAAzwEAACoAAABw'
    'cHQvbm90ZXNTbGlkZXMvX3JlbHMvbm90ZXNTbGlkZTEueG1sLnJlbHO1kT1OxDAUhK9ivZ44jrxJhNa7DQ0FzWov4HVeEmv9J/st'
    'Cmej4EhcAQkoEkRBQzsz+vRJ8/76tj8u3rFnzMXGoEBUNTAMJg42TApuNN71cDzsT+g02RjKbFNhi3ehKJiJ0j3nxczodaliwrB4'
    'N8bsNZUq5oknba56Qt7UdcvzmgFbJju/JPwLMY6jNfgQzc1joF/AvDg7ILCzzhOSAp4SfWXflagW74A9DgpOQtRD23cXLUUjpZHA'
    '+L95hUhYnnQhzD/sVs1mtjZF0aC8dEPb7HrZ7fpPU7655fABUEsDBBQAAAAIAI6JKF0Tq5873QAAAM8BAAAqAAAAcHB0L25vdGVz'
    'U2xpZGVzL19yZWxzL25vdGVzU2xpZGUyLnhtbC5yZWxztZE9TsQwFISvYr2e2E7CkqzWuw0NBc1qL+DYL4mF/2R7UTgbBUfiCkhA'
    'kSAKGtqZ0adPmvfXt8NpcZY8Y8omeAG8YkDQq6CNnwRcy3jTwel4OKOVxQSfZxMzWZz1WcBcStxTmtWMTuYqRPSLs2NITpZchTTR'
    'KNWTnJDWjO1oWjNgyySXl4h/IYZxNArvg7o69OUXMM3WaARykWnCIoDGWL6y76quFmeBPGgBZ9axsdV3vbrFod11DRD6b14+FMyP'
    'MhdMP+xWzWbGV6ZNLdXQ874bOG91oz5N6eaW4wdQSwMEFAAAAAgAjokoXSJy0RfdAAAAzwEAACoAAABwcHQvbm90ZXNTbGlkZXMv'
    'X3JlbHMvbm90ZXNTbGlkZTMueG1sLnJlbHO1kT1Ow0AQha+ymh7vOnFMEsVJQ0NBE+UCy87YXrF/2tkgczYKjsQVkIDCRhQ0tO89'
    'ffqk9/76djhN3olnymxj6KCuFAgKJqINQwfX0t9s4XQ8nMnpYmPg0SYWk3eBOxhLSXsp2YzkNVcxUZi862P2unAV8yCTNk96ILlS'
    'qpV5zoAlU1xeEv2FGPveGrqL5uoplF/Akp1FAnHReaDSgUypfGXf1bqavANxjx2cVV3vCFvVmkdszBpByH/zCrEQP2gulH/YzZrF'
    'rJ6Zbm5xqxBbNLtVY5rNp6lc3HL8AFBLAwQUAAAACACOiShdJ3QAU9wAAADPAQAAKgAAAHBwdC9ub3Rlc1NsaWRlcy9fcmVscy9u'
    'b3Rlc1NsaWRlNC54bWwucmVsc7WRPU7EMBSEr2K9njgxZgmr9W5DQ0Gz2gt4nefEwn+y36JwNgqOxBWQgCJBFDS0M6NPnzTvr2+7'
    'wxw8e8ZSXYoKuqYFhtGkwcVRwYXsVQ+H/e6IXpNLsU4uVzYHH6uCiShvOa9mwqBrkzLGOXibStBUm1RGnrV50iNy0bYbXpYMWDPZ'
    '6SXjX4jJWmfwPplLwEi/gHn1bkBgJ11GJAU8Z/rKvivZzMEDexgUHM+I/Xm4a8VGW3krDDD+b14xEdZHXQnLD7tFs5p1C1MppL3W'
    '2na9sHIQN5+mfHXL/gNQSwMEFAAAAAgAjokoXS12giDdAAAAzwEAACoAAABwcHQvbm90ZXNTbGlkZXMvX3JlbHMvbm90ZXNTbGlk'
    'ZTUueG1sLnJlbHO1kTtOxDAURbdivZ7YmXwU0HimoZmCZjQbcOznxMI/2Z5RWBsFS2ILSECRIAoa2nuvjo5031/f9sfFWXLDlE3w'
    'HOqKAUEvgzJ+4nAt+m6A42F/RiuKCT7PJmayOOszh7mU+EBpljM6kasQ0S/O6pCcKLkKaaJRyGcxId0x1tO0ZsCWSS4vEf9CDFob'
    'iY9BXh368guYZmsUArmINGHhQGMsX9l31VWLs0BOisN56MdubCVTom3aelRA6L95+VAwP4lcMP2wWzWbWb0y3emhbxRTQtZNK7v7'
    'T1O6ueXwAVBLAwQUAAAACACOiShdTXL/k4ABAACkBgAAHwAAAHBwdC9fcmVscy9wcmVzZW50YXRpb24ueG1sLnJlbHPF1c1q3DAQ'
    'AOBXMbp39eOxvCpxcsklh0JJ8wIjabRralvCUsrm2XroI/UVCk0p2mWhvZi96DAzDB+aEfr5/cfdw2memm+05jEuA5M7wRpaXPTj'
    'chjYawkf9uzh/u6ZJixjXPJxTLk5zdOSB3YsJX3kPLsjzZh3MdFymqcQ1xlL3sX1wBO6r3ggroTQfK17sPOezctbov/pGEMYHT1G'
    '9zrTUq405uVIM7HmBdcDlYHxlMp77P2Uu9M8sebJD+zZdiEoF0i5vQQlBWv4Zqw8jZ4+YS60XuCqzFlZLfVCahGM98YaCMZvKV1i'
    'oXxVWmXOymqpBFBOKqnQeuiM2VKaVsqf15jyhfNvvHKhAG32whoCA9jqLV0F7URfyttEl7IqU9nAQtB9Z2wnCbrebb6H1zbwT6qe'
    'ZS+xJ2k9oTIgoL2lS9WzbHXfggRjCYHgpvfVVi4h0aG2wglpwYO/pQsqlwK7h1Yr5bEFA5u+yX+5unrvtUPbQxuwa8F5/O3iZ3/N'
    '/S9QSwMEFAAAAAgAjokoXQaWkE29AAAANwEAACwAAABwcHQvc2xpZGVMYXlvdXRzL19yZWxzL3NsaWRlTGF5b3V0MS54bWwucmVs'
    'c43PPW7CQBCG4auspsdjQ4Qs5DVNmhRpEBdY1mN7lf3TzhCZs1FwJK5Ai6UUqb9Xj/Q974/uuASvfqmwS1FDU9WgKNo0uDhpuMq4'
    'aeHYdyfyRlyKPLvMagk+soZZJB8Q2c4UDFcpU1yCH1MJRrhKZcJs7I+ZCLd1vcfybsDaVOdbpv+IaRydpc9kr4Gi/AEjezfQt2Gh'
    'AupsykSiAXOW92WVNdUSPKivQcPp0jYtWbOjrb181O0OFPYdru73L1BLAwQUAAAACACOiShd9iUAYeUAAADbAQAALAAAAHBwdC9z'
    'bGlkZU1hc3RlcnMvX3JlbHMvc2xpZGVNYXN0ZXIxLnhtbC5yZWxztZG9TsMwFIVfxbo7cZyGNqC6XViQYKn6Asa+SSz8J/sGpc/G'
    'wCPxCkgtQyN1YGE5yzn69Enn+/Nru5+9Yx+Yi41BgqhqYBh0NDYMEibq7zrY77YHdIpsDGW0qbDZu1AkjETpkfOiR/SqVDFhmL3r'
    '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'
    'PAAAcHB0L25vdGVzTWFzdGVycy9fcmVscy9ub3Rlc01hc3RlcjEueG1sLnJlbHNQSwECFAMUAAAACACOiShdaNTRINwAAADPAQAA'
    'KgAAAAAAAAAAAAAApIEcPQAAcHB0L25vdGVzU2xpZGVzL19yZWxzL25vdGVzU2xpZGUxLnhtbC5yZWxzUEsBAhQDFAAAAAgAjoko'
    'XROrnzvdAAAAzwEAACoAAAAAAAAAAAAAAKSBQD4AAHBwdC9ub3Rlc1NsaWRlcy9fcmVscy9ub3Rlc1NsaWRlMi54bWwucmVsc1BL'
    'AQIUAxQAAAAIAI6JKF0ictEX3QAAAM8BAAAqAAAAAAAAAAAAAACkgWU/AABwcHQvbm90ZXNTbGlkZXMvX3JlbHMvbm90ZXNTbGlk'
    'ZTMueG1sLnJlbHNQSwECFAMUAAAACACOiShdJ3QAU9wAAADPAQAAKgAAAAAAAAAAAAAApIGKQAAAcHB0L25vdGVzU2xpZGVzL19y'
    'ZWxzL25vdGVzU2xpZGU0LnhtbC5yZWxzUEsBAhQDFAAAAAgAjokoXS12giDdAAAAzwEAACoAAAAAAAAAAAAAAKSBrkEAAHBwdC9u'
    'b3Rlc1NsaWRlcy9fcmVscy9ub3Rlc1NsaWRlNS54bWwucmVsc1BLAQIUAxQAAAAIAI6JKF1Ncv+TgAEAAKQGAAAfAAAAAAAAAAAA'
    'AACkgdNCAABwcHQvX3JlbHMvcHJlc2VudGF0aW9uLnhtbC5yZWxzUEsBAhQDFAAAAAgAjokoXQaWkE29AAAANwEAACwAAAAAAAAA'
    'AAAAAKSBkEQAAHBwdC9zbGlkZUxheW91dHMvX3JlbHMvc2xpZGVMYXlvdXQxLnhtbC5yZWxzUEsBAhQDFAAAAAgAjokoXfYlAGHl'
    'AAAA2wEAACwAAAAAAAAAAAAAAKSBl0UAAHBwdC9zbGlkZU1hc3RlcnMvX3JlbHMvc2xpZGVNYXN0ZXIxLnhtbC5yZWxzUEsBAhQD'
    'FAAAAAgAjokoXbUW9knfAAAA3gEAACAAAAAAAAAAAAAAAKSBxkYAAHBwdC9zbGlkZXMvX3JlbHMvc2xpZGUxLnhtbC5yZWxzUEsB'
    'AhQDFAAAAAgAjokoXSMTNJ7fAAAA3gEAACAAAAAAAAAAAAAAAKSB40cAAHBwdC9zbGlkZXMvX3JlbHMvc2xpZGUyLnhtbC5yZWxz'
    'UEsBAhQDFAAAAAgAjokoXd1Nm7XeAAAA3gEAACAAAAAAAAAAAAAAAKSBAEkAAHBwdC9zbGlkZXMvX3JlbHMvc2xpZGUzLnhtbC5y'
    'ZWxzUEsBAhQDFAAAAAgAjokoXQXveRTeAAAA3gEAACAAAAAAAAAAAAAAAKSBHEoAAHBwdC9zbGlkZXMvX3JlbHMvc2xpZGU0Lnht'
    'bC5yZWxzUEsBAhQDFAAAAAgAjokoXYIQxaHfAAAA3gEAACAAAAAAAAAAAAAAAKSBOEsAAHBwdC9zbGlkZXMvX3JlbHMvc2xpZGU1'
    'LnhtbC5yZWxzUEsBAhQDFAAAAAgAjokoXax+WLmaAQAA9AsAABMAAAAAAAAAAAAAAKSBVUwAAFtDb250ZW50X1R5cGVzXS54bWxQ'
    'SwUGAAAAACUAJQDvCgAAIE4AAAAA'
)
