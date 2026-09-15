"""Explicit Google Calendar actions scoped to an Egasis workspace.

API references:
https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query
https://developers.google.com/workspace/calendar/api/v3/reference/events/insert
https://developers.google.com/identity/protocols/oauth2/web-server#offline

This service creates an owner's calendar reservation without inviting attendees
or asserting that a prospect accepted. Callers must require the owner's explicit
confirmation. No worker invokes these actions automatically.
"""
import hashlib
import hmac
import json
import math
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from sqlalchemy import select, text

from .models import Connection, Contact, Event, Meeting, Workspace


GOOGLE_API = 'https://www.googleapis.com/calendar/v3'
GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token'


class CalendarError(RuntimeError):
    def __init__(self, message, status_code=422):
        super().__init__(message)
        self.status_code = status_code


def _instant(value):
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ValueError()
            parsed = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        else:
            raise ValueError()
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        raise CalendarError('Usá una fecha válida con zona horaria o segundos UTC.') from None


def _rfc(value):
    return _instant(value).isoformat().replace('+00:00', 'Z')


class CalendarService:
    def __init__(self, factory, vault, client=None):
        self.factory, self.vault, self.client = factory, vault, client

    @contextmanager
    def _transaction(self):
        with self.factory() as db:
            if db.bind.dialect.name == 'sqlite':
                db.execute(text('BEGIN IMMEDIATE'))
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    @contextmanager
    def _http(self):
        if self.client is not None:
            yield self.client
        else:
            with httpx.Client(timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False) as client:
                yield client

    def _configuration(self, db, workspace_id):
        if not db.get(Workspace, workspace_id):
            raise CalendarError('Espacio de trabajo inexistente.', 404)
        row = db.scalar(select(Connection).where(Connection.workspace_id == workspace_id,
                                                Connection.provider == 'google_calendar').with_for_update())
        if not row:
            raise CalendarError('Conectá Google Calendar antes de consultar o reservar.', 409)
        try:
            credentials = json.loads(self.vault.decrypt(row.secret))
        except Exception:
            raise CalendarError('La conexión de Google Calendar debe configurarse nuevamente.', 409) from None
        if not isinstance(credentials, dict):
            raise CalendarError('La conexión de Google Calendar tiene un formato inválido.', 409)
        calendar_id = credentials.get('calendar_id') or 'primary'
        if not isinstance(calendar_id, str) or not calendar_id.strip() or len(calendar_id) > 1024:
            raise CalendarError('El identificador del calendario no es válido.', 409)
        credentials['calendar_id'] = calendar_id.strip()
        for name in ('access_token', 'refresh_token', 'client_id', 'client_secret'):
            if credentials.get(name) is not None and not isinstance(credentials[name], str):
                raise CalendarError('La conexión de Google Calendar tiene un formato inválido.', 409)
        return row, credentials

    def _json(self, response):
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError:
            raise CalendarError('Google Calendar devolvió una respuesta que no se pudo verificar.', 502) from None

    def _refresh(self, client, row, credentials):
        client_id = credentials.get('client_id') or os.getenv('EGASIS_GOOGLE_CLIENT_ID', '')
        client_secret = credentials.get('client_secret') or os.getenv('EGASIS_GOOGLE_CLIENT_SECRET', '')
        refresh_token = credentials.get('refresh_token')
        if not (client_id and client_secret and refresh_token):
            raise CalendarError('La sesión de Google venció. Reconectá el calendario.', 401)
        response = client.post(GOOGLE_TOKEN, data={'client_id': client_id, 'client_secret': client_secret,
                               'refresh_token': refresh_token, 'grant_type': 'refresh_token'}, follow_redirects=False)
        if response.status_code != 200:
            raise CalendarError('No se pudo renovar la sesión de Google. Reconectá el calendario.', 401)
        fresh = self._json(response)
        if not isinstance(fresh.get('access_token'), str) or not fresh['access_token']:
            raise CalendarError('Google no devolvió un token válido.', 502)
        credentials['access_token'] = fresh['access_token']
        if isinstance(fresh.get('refresh_token'), str) and fresh['refresh_token']:
            credentials['refresh_token'] = fresh['refresh_token']
        row.secret = self.vault.encrypt(json.dumps(credentials))

    def _request(self, client, row, credentials, method, path, **kwargs):
        # Calendar identifiers are escaped path segments; host and token endpoint
        # are fixed. Tokens never go in URLs or user-visible error messages.
        try:
            if not credentials.get('access_token'):
                self._refresh(client, row, credentials)
            response = client.request(method, GOOGLE_API + path,
                                      headers={'Authorization': 'Bearer ' + credentials['access_token']},
                                      follow_redirects=False, **kwargs)
            if response.status_code == 401:
                self._refresh(client, row, credentials)
                response = client.request(method, GOOGLE_API + path,
                                          headers={'Authorization': 'Bearer ' + credentials['access_token']},
                                          follow_redirects=False, **kwargs)
            return response
        except httpx.RequestError:
            raise CalendarError('No se pudo verificar el resultado en Google Calendar. Reintentá la misma operación para conciliarla.', 503) from None

    def _require_success(self, response, allowed=(200,)):
        if response.status_code not in allowed:
            status = response.status_code
            message = 'Google Calendar no confirmó la operación.'
            if status in {401, 403}:
                message = 'Revisá la conexión y los permisos de Google Calendar.'
            raise CalendarError(message + f' (HTTP {status})', status if status in {401, 403, 409, 429} else 502)

    def _busy(self, client, row, credentials, calendar_id, start, end):
        start, end = _instant(start), _instant(end)
        if end <= start or (end - start).total_seconds() > 93 * 86400:
            raise CalendarError('Elegí un período positivo de hasta 93 días.')
        response = self._request(client, row, credentials, 'POST', '/freeBusy', json={
            'timeMin': _rfc(start), 'timeMax': _rfc(end), 'timeZone': 'UTC', 'items': [{'id': calendar_id}],
        })
        self._require_success(response)
        calendars = self._json(response).get('calendars')
        calendar = calendars.get(calendar_id) if isinstance(calendars, dict) else None
        if not isinstance(calendar, dict) or calendar.get('errors') or not isinstance(calendar.get('busy'), list):
            raise CalendarError('Google no pudo verificar la disponibilidad de este calendario.', 502)
        busy = []
        try:
            for interval in calendar['busy']:
                beginning, ending = _instant(interval['start']), _instant(interval['end'])
                if ending <= beginning:
                    raise ValueError()
                busy.append({'start': _rfc(beginning), 'end': _rfc(ending)})
        except (KeyError, TypeError, ValueError, CalendarError):
            raise CalendarError('Google devolvió intervalos de disponibilidad inválidos.', 502) from None
        return {'calendar_id': calendar_id, 'start': _rfc(start), 'end': _rfc(end),
                'busy': sorted(busy, key=lambda item: item['start'])}

    def availability(self, workspace_id, start, end):
        with self._transaction() as db, self._http() as client:
            row, credentials = self._configuration(db, workspace_id)
            return self._busy(client, row, credentials, credentials['calendar_id'], start, end)

    def _meeting(self, db, workspace_id, meeting_id):
        meeting = db.scalar(select(Meeting).where(Meeting.id == meeting_id, Meeting.workspace_id == workspace_id).with_for_update())
        if not meeting:
            raise CalendarError('Reunión inexistente.', 404)
        contact = db.scalar(select(Contact).where(Contact.id == meeting.contact_id, Contact.workspace_id == workspace_id))
        if not contact:
            raise CalendarError('El contacto de la reunión no pertenece a este espacio.', 409)
        return meeting, contact

    def _event_id(self, workspace_id, meeting_id):
        return f'egasis{workspace_id}meeting{meeting_id}'

    def _binding(self, workspace_id, meeting_id):
        return hmac.new(self.vault.key, f'egasis-calendar:{workspace_id}:{meeting_id}'.encode(), hashlib.sha256).hexdigest()

    def _intent(self, db, workspace_id, meeting_id):
        rows = db.scalars(select(Event).where(Event.workspace_id == workspace_id, Event.kind == 'calendar.intent').order_by(Event.id.desc()))
        for row in rows:
            try:
                record = json.loads(row.detail)
            except (ValueError, TypeError):
                continue
            if isinstance(record, dict) and record.get('meeting_id') == meeting_id:
                return record
        return None

    def _prepare_intent(self, workspace_id, meeting_id):
        # Persist the selected calendar before contacting Google. A timeout or a
        # later connection change must not create the same meeting elsewhere.
        with self._transaction() as db:
            meeting, _ = self._meeting(db, workspace_id, meeting_id)
            _, credentials = self._configuration(db, workspace_id)
            if meeting.status == 'cancelled':
                raise CalendarError('Creá una nueva reunión para reservar después de una cancelación.', 409)
            if meeting.external_id and meeting.external_id != self._event_id(workspace_id, meeting_id):
                raise CalendarError('Esta reunión está vinculada a otro evento externo.', 409)
            if not self._intent(db, workspace_id, meeting_id):
                if meeting.external_id:
                    raise CalendarError('Falta la vinculación verificable del calendario de esta reunión.', 409)
                db.add(Event(workspace_id=workspace_id, kind='calendar.intent', detail=json.dumps({
                    'meeting_id': meeting_id, 'calendar_id': credentials['calendar_id'],
                    'event_id': self._event_id(workspace_id, meeting_id),
                }, sort_keys=True)))

    def _verify_event(self, event, meeting, check_time=True):
        expected_id = self._event_id(meeting.workspace_id, meeting.id)
        # Google may return only an ID for a cancelled event. The previously
        # persisted binding is sufficient to reconcile that known tombstone.
        if event.get('status') == 'cancelled' and event.get('id') == expected_id and meeting.external_id == expected_id:
            return
        properties = event.get('extendedProperties')
        private = properties.get('private') if isinstance(properties, dict) else None
        if not isinstance(private, dict):
            private = {}
        if (event.get('id') != expected_id or private.get('egasis_workspace_id') != str(meeting.workspace_id)
                or private.get('egasis_meeting_id') != str(meeting.id)
                or private.get('egasis_binding') != self._binding(meeting.workspace_id, meeting.id)):
            raise CalendarError('El evento de Google no coincide con la reunión registrada. No se modificó.', 409)
        if check_time and event.get('status') != 'cancelled':
            try:
                start = _instant(event['start']['dateTime']).timestamp()
                end = _instant(event['end']['dateTime']).timestamp()
                matches = abs(start - meeting.starts_at) < 0.001 and abs(end - (meeting.starts_at + meeting.duration_minutes * 60)) < 0.001
            except (KeyError, TypeError, CalendarError):
                matches = False
            if not matches:
                raise CalendarError('El horario del evento de Google cambió. Revisalo antes de confirmar en Egasis.', 409)

    def _save_result(self, db, meeting, calendar_id, status, event_id=None):
        changed = meeting.status != status or (event_id is not None and meeting.external_id != event_id)
        meeting.status = status
        if event_id is not None:
            meeting.external_id = event_id
        if changed:
            db.add(Event(workspace_id=meeting.workspace_id, kind='calendar.' + status, detail=json.dumps({
                'meeting_id': meeting.id, 'event_id': meeting.external_id, 'calendar_id': calendar_id,
                'confirmation': 'provider_calendar',
            }, sort_keys=True)))
        return {'meeting_id': meeting.id, 'external_id': meeting.external_id, 'status': status, 'calendar_id': calendar_id}

    def create_event(self, workspace_id, meeting_id):
        self._prepare_intent(workspace_id, meeting_id)
        with self._transaction() as db, self._http() as client:
            meeting, contact = self._meeting(db, workspace_id, meeting_id)
            row, credentials = self._configuration(db, workspace_id)
            if meeting.status == 'cancelled':
                raise CalendarError('La reunión está cancelada.', 409)
            if not isinstance(meeting.duration_minutes, int) or not 1 <= meeting.duration_minutes <= 1440:
                raise CalendarError('La duración de la reunión no es válida.')
            intent = self._intent(db, workspace_id, meeting_id)
            calendar_id, event_id = intent['calendar_id'], intent['event_id']
            path = '/calendars/' + quote(calendar_id, safe='') + '/events'
            existing = self._request(client, row, credentials, 'GET', path + '/' + event_id)
            if existing.status_code == 200:
                event = self._json(existing)
            else:
                if existing.status_code != 404:
                    self._require_success(existing)
                if meeting.external_id:
                    raise CalendarError('El evento vinculado ya no está disponible. Revisá el calendario; no se creó otro.', 409)
                if _instant(meeting.starts_at).timestamp() <= time.time():
                    raise CalendarError('Elegí un horario futuro para la reunión.')
                ends_at = meeting.starts_at + meeting.duration_minutes * 60
                if self._busy(client, row, credentials, calendar_id, meeting.starts_at, ends_at)['busy']:
                    raise CalendarError('Ese horario ya está ocupado en Google Calendar.', 409)
                body = {
                    'id': event_id, 'summary': 'Egasis · ' + (contact.name or contact.company or contact.email),
                    'description': meeting.notes or '', 'location': meeting.location or '',
                    'start': {'dateTime': _rfc(meeting.starts_at)}, 'end': {'dateTime': _rfc(ends_at)},
                    'extendedProperties': {'private': {'egasis_workspace_id': str(workspace_id),
                        'egasis_meeting_id': str(meeting_id), 'egasis_binding': self._binding(workspace_id, meeting_id)}},
                }
                created = self._request(client, row, credentials, 'POST', path, params={'sendUpdates': 'none'}, json=body)
                if created.status_code == 409:
                    created = self._request(client, row, credentials, 'GET', path + '/' + event_id)
                self._require_success(created, (200, 201))
                event = self._json(created)
            self._verify_event(event, meeting)
            if event.get('status') == 'cancelled':
                return self._save_result(db, meeting, calendar_id, 'cancelled', event_id)
            if event.get('status') != 'confirmed':
                raise CalendarError('Google todavía no confirmó la reserva.', 409)
            return self._save_result(db, meeting, calendar_id, 'confirmed', event_id)

    def cancel_event(self, workspace_id, meeting_id):
        with self._transaction() as db, self._http() as client:
            meeting, _ = self._meeting(db, workspace_id, meeting_id)
            intent = self._intent(db, workspace_id, meeting_id)
            if not intent:
                raise CalendarError('La reunión no tiene una vinculación verificable con Google Calendar.', 409)
            event_id, calendar_id = intent['event_id'], intent['calendar_id']
            if meeting.external_id and meeting.external_id != event_id:
                raise CalendarError('La reunión está vinculada a otro evento.', 409)
            if meeting.status == 'cancelled':
                return self._save_result(db, meeting, calendar_id, 'cancelled')
            row, credentials = self._configuration(db, workspace_id)
            path = '/calendars/' + quote(calendar_id, safe='') + '/events/' + event_id
            response = self._request(client, row, credentials, 'GET', path)
            if response.status_code not in {404, 410}:
                self._require_success(response)
                event = self._json(response)
                self._verify_event(event, meeting, check_time=False)
                if event.get('status') != 'cancelled':
                    deleted = self._request(client, row, credentials, 'DELETE', path, params={'sendUpdates': 'none'})
                    self._require_success(deleted, (200, 204, 404, 410))
            return self._save_result(db, meeting, calendar_id, 'cancelled', meeting.external_id)
