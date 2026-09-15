"""Optional, workspace-owned Apollo previews; this module never imports contacts.

Official contracts checked 2026-09-08:
https://docs.apollo.io/reference/people-api-search
https://docs.apollo.io/reference/people-enrichment
https://docs.apollo.io/docs/find-people-using-filters
https://docs.apollo.io/reference/rate-limits
https://docs.apollo.io/docs/api-pricing

Search does not reveal emails. One-person enrichment is a separate explicit
credit-consuming operation. Personal emails, phone reveal and waterfall are off.
An operator must separately establish Apollo's integration authorization before
setting EGASIS_APOLLO_INTEGRATION_AUTHORIZED=true; possession of a key is not that
authorization. Only the requesting workspace's encrypted Connection is used.
"""
from collections.abc import Sequence
import ipaddress
import json
import os
import re
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select

from .models import Campaign, Connection, Contact, Event, Suppression, Workspace


BASE_URL = 'https://api.apollo.io'
SEARCH_PATH = '/api/v1/mixed_people/api_search'
ENRICH_PATH = '/api/v1/people/match'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SENIORITIES = frozenset(('owner', 'founder', 'c_suite', 'partner', 'vp', 'head', 'director', 'manager', 'senior', 'entry', 'intern'))
RATE_HEADERS = ('x-rate-limit-minute', 'x-rate-limit-hourly', 'x-rate-limit-24-hour',
    'x-minute-usage', 'x-hourly-usage', 'x-24-hour-usage', 'x-minute-requests-left',
    'x-hourly-requests-left', 'x-24-hour-requests-left')


class SourceError(ValueError):
    """Safe API-facing failure; provider response bodies and keys are never exposed."""

    def __init__(self, message, *, code='source_error', http_status=None,
                 retry_after_seconds=None, credit_outcome_uncertain=False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.credit_outcome_uncertain = credit_outcome_uncertain


class SourceUnavailable(SourceError):
    pass


class SourceRateLimited(SourceError):
    pass


def _clean_text(value, limit=200):
    return value.strip()[:limit] if isinstance(value, str) else ''


def _strings(values, name, maximum=25):
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or len(values) > maximum:
        raise SourceError(f'{name}: usá una lista de hasta {maximum} valores.', code='invalid_filters')
    output = []
    for value in values:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 150 or any(ord(char) < 32 for char in value):
            raise SourceError(f'{name}: hay un valor vacío o inválido.', code='invalid_filters')
        if value.strip() not in output:
            output.append(value.strip())
    return output


def _domain(value):
    return bool(re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', value))


def _website(organization):
    candidate = _clean_text(organization.get('website_url'), 1000)
    if not candidate:
        domain = _clean_text(organization.get('primary_domain'), 253).lower()
        candidate = 'https://' + domain if _domain(domain) else ''
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return ''
        hostname = parsed.hostname.lower()
        if hostname == 'localhost' or hostname.endswith(('.localhost', '.local', '.invalid', '.test')):
            return ''
        try:
            if not ipaddress.ip_address(hostname).is_global:
                return ''
        except ValueError:
            if not _domain(hostname):
                return ''
        return candidate
    except ValueError:
        return ''


def _email(value):
    value = _clean_text(value, 255).lower()
    if len(value) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+", value):
        return None
    local, domain = value.rsplit('@', 1)
    if not _domain(domain) or local.startswith(('email_not_unlocked', 'email_not_revealed')) or local.startswith('.') or local.endswith('.') or '..' in local:
        return None
    if domain in ('example.com', 'example.net', 'example.org') or domain.endswith(('.example', '.invalid', '.test', '.example.com', '.example.net', '.example.org')):
        return None
    return value


def _rate_limits(headers):
    output = {}
    for name in RATE_HEADERS:
        value = headers.get(name, '')
        if re.fullmatch(r'\d{1,12}', value):
            output[name] = int(value)
    return output


class ApolloSource:
    def __init__(self, session_factory, vault, transport: httpx.BaseTransport | None = None):
        self.sessions = session_factory
        self.vault = vault
        self.transport = transport

    @staticmethod
    def _authorized():
        return os.getenv('EGASIS_APOLLO_INTEGRATION_AUTHORIZED', '').strip().lower() == 'true'

    def readiness(self, workspace_id: int) -> dict:
        """Read local readiness without decrypting secrets or contacting Apollo."""
        with self.sessions() as db:
            if not db.get(Workspace, workspace_id):
                raise SourceError('No se encontró este espacio de trabajo.', code='workspace_not_found')
            configured = bool(db.scalar(select(Connection.id).where(Connection.workspace_id == workspace_id, Connection.provider == 'apollo')))
        authorized = self._authorized()
        return {'provider': 'apollo', 'integration_authorized': authorized, 'key_configured': configured,
            'available': authorized and configured,
            'api_key_access': ['api/v1/mixed_people/api_search', 'api/v1/people/match'],
            'oauth_scopes': ['mixed_people_api_search', 'people_match'],
            'search_reveals_email': False, 'enrichment_may_consume_credits': True}

    def _key(self, workspace_id, campaign_id):
        with self.sessions() as db:
            campaign = db.scalar(select(Campaign).where(Campaign.id == campaign_id, Campaign.workspace_id == workspace_id))
            if not campaign:
                raise SourceError('La campaña no pertenece a este espacio.', code='campaign_not_found')
            connection = db.scalar(select(Connection).where(Connection.workspace_id == workspace_id, Connection.provider == 'apollo'))
        if not self._authorized():
            raise SourceUnavailable('Apollo requiere que Egasis tenga autorización de integración antes de habilitar búsquedas.', code='integration_not_authorized')
        if not connection:
            raise SourceUnavailable('Conectá una clave de Apollo propia de este espacio de trabajo.', code='key_missing')
        try:
            key = self.vault.decrypt(connection.secret)
        except Exception:
            raise SourceUnavailable('La clave de Apollo de este espacio no se pudo descifrar. Volvé a conectarla.', code='key_unreadable') from None
        if not isinstance(key, str) or not key.strip() or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise SourceUnavailable('La clave de Apollo configurada no es válida.', code='key_invalid')
        return key

    def _event(self, workspace_id, kind, detail):
        with self.sessions() as db:
            db.add(Event(workspace_id=workspace_id, kind=kind, detail=json.dumps(detail, ensure_ascii=False)))
            db.commit()

    def _request(self, workspace_id, campaign_id, path, payload, key):
        enrichment = path == ENRICH_PATH
        operation = 'enrich' if enrichment else 'search'
        self._event(workspace_id, f'apollo.{operation}_requested', {'campaign_id': campaign_id,
            'person_id': payload.get('id'), 'page': payload.get('page'), 'may_consume_credits': enrichment})
        try:
            with httpx.Client(transport=self.transport, timeout=httpx.Timeout(20.0, connect=10.0), trust_env=False, follow_redirects=False) as client:
                with client.stream('POST', BASE_URL + path, json=payload,
                        headers={'x-api-key': key, 'Accept': 'application/json', 'Content-Type': 'application/json', 'Cache-Control': 'no-cache'}) as response:
                    rates = _rate_limits(response.headers)
                    status = response.status_code
                    if status == 429:
                        retry = response.headers.get('retry-after', '')
                        seconds = int(retry) if re.fullmatch(r'\d{1,9}', retry) else None
                        raise SourceRateLimited('Apollo alcanzó el límite de solicitudes. Esperá el plazo indicado por tu cuenta antes de repetir.',
                            code='rate_limited', http_status=status, retry_after_seconds=seconds)
                    if status in (401, 403):
                        reason = 'La clave de Apollo fue rechazada. Revisá la conexión de este espacio.' if status == 401 else 'Apollo no habilita este endpoint para tu clave o plan. Revisá sus permisos de búsqueda/enriquecimiento.'
                        raise SourceUnavailable(reason, code='authentication_failed' if status == 401 else 'access_denied', http_status=status)
                    if status == 422:
                        raise SourceError('Apollo rechazó los filtros o el identificador. Revisá los valores enviados.', code='invalid_provider_request', http_status=status)
                    if status < 200 or status >= 300:
                        raise SourceUnavailable('Apollo no confirmó la operación. Revisá el uso de créditos antes de repetir un enriquecimiento.' if enrichment else 'Apollo no está disponible para esta búsqueda. Intentá más tarde.',
                            code='provider_unavailable', http_status=status, credit_outcome_uncertain=enrichment and status >= 500)
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise SourceError('Apollo devolvió una respuesta demasiado grande.', code='invalid_response', credit_outcome_uncertain=enrichment)
                    try:
                        data = json.loads(content)
                    except (ValueError, UnicodeError):
                        raise SourceError('Apollo devolvió una respuesta que no se pudo interpretar.', code='invalid_response', credit_outcome_uncertain=enrichment) from None
                    if not isinstance(data, dict) or data.get('error') or data.get('error_code'):
                        raise SourceError('Apollo no devolvió un resultado válido para esta operación.', code='invalid_response', credit_outcome_uncertain=enrichment)
        except httpx.HTTPError:
            self._event(workspace_id, f'apollo.{operation}_failed', {'campaign_id': campaign_id, 'code': 'network_error', 'credit_outcome_uncertain': enrichment})
            raise SourceUnavailable('No se pudo confirmar la respuesta de Apollo. El enriquecimiento puede haber consumido créditos; verificá tu cuenta antes de repetir.' if enrichment else 'No se pudo conectar con Apollo. No se generaron contactos.',
                code='network_error', credit_outcome_uncertain=enrichment) from None
        except SourceError as exc:
            self._event(workspace_id, f'apollo.{operation}_failed', {'campaign_id': campaign_id, 'code': exc.code, 'http_status': exc.http_status,
                'retry_after_seconds': exc.retry_after_seconds, 'credit_outcome_uncertain': exc.credit_outcome_uncertain})
            raise
        self._event(workspace_id, f'apollo.{operation}_completed', {'campaign_id': campaign_id, 'http_status': status, 'rate_limits': rates})
        return data, rates

    def _normalize(self, workspace_id, campaign_id, person, *, enriched, requested_id=None, match_confidence=None):
        provider_id = _clean_text(person.get('id') or person.get('person_id'), 80)
        if not provider_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', provider_id):
            raise SourceError('Apollo devolvió una persona sin identificador válido.', code='invalid_response')
        if requested_id is not None and provider_id != requested_id:
            raise SourceError('Apollo devolvió un identificador diferente. No se incorporó el contacto.', code='identity_mismatch', credit_outcome_uncertain=enriched)
        organization = person.get('organization') if isinstance(person.get('organization'), dict) else {}
        first = _clean_text(person.get('first_name'), 160)
        last = _clean_text(person.get('last_name'), 160) if enriched else ''
        name = (_clean_text(person.get('name'), 160) or ' '.join(part for part in (first, last) if part)) if enriched else first
        display = name if enriched else ' '.join(part for part in (first, _clean_text(person.get('last_name_obfuscated'), 160)) if part)
        email = _email(person.get('email')) if enriched else None
        email_status = _clean_text(person.get('email_status'), 80).lower() if enriched else 'not_revealed'
        confidence = _clean_text(match_confidence or person.get('match_confidence'), 30).lower() or 'unknown'
        status = 'preview_no_email' if not enriched else 'no_email' if not email else 'ready' if email_status == 'verified' and confidence not in ('none', 'low', 'medium') else 'review_email'
        if email:
            with self.sessions() as db:
                if db.scalar(select(Suppression.id).where(Suppression.workspace_id == workspace_id, func.lower(Suppression.email) == email)):
                    status = 'suppressed'
                elif db.scalar(select(Contact.id).where(Contact.workspace_id == workspace_id, Contact.campaign_id == campaign_id, func.lower(Contact.email) == email)):
                    status = 'duplicate'
        return {'provider_id': provider_id, 'name': name, 'display_name': display, 'company': _clean_text(organization.get('name')),
            'title': _clean_text(person.get('title')), 'website': _website(organization), 'email': email,
            'email_status': email_status, 'has_email': bool(email) if enriched else person.get('has_email') is True,
            'match_confidence': confidence, 'status': status, 'importable': status == 'ready', 'source': 'apollo',
            'source_metadata': {'provider': 'apollo', 'person_id': provider_id, 'endpoint': ENRICH_PATH if enriched else SEARCH_PATH,
                'retrieved_at': time.time(), 'email_revealed': bool(email), 'email_status_reported_by': 'apollo' if enriched else None,
                'last_refreshed_at': _clean_text(person.get('last_refreshed_at'), 100), 'preview_only': not enriched}}

    def search(self, workspace_id: int, campaign_id: int, *, keywords: str = '', titles: Sequence[str] = (),
               person_locations: Sequence[str] = (), organization_locations: Sequence[str] = (),
               seniorities: Sequence[str] = (), employee_ranges: Sequence[str] = (), domains: Sequence[str] = (),
               page: int = 1, per_page: int = 25, include_similar_titles: bool = False) -> dict:
        """Return one preview page. Never reveal email, enrich, import, or send mail."""
        if type(page) is not int or not 1 <= page <= 500 or type(per_page) is not int or not 1 <= per_page <= 100:
            raise SourceError('Página válida: 1–500. Resultados por página: 1–100.', code='invalid_filters')
        if not isinstance(keywords, str) or len(keywords) > 250 or any(ord(char) < 32 for char in keywords) or type(include_similar_titles) is not bool:
            raise SourceError('Palabras clave o coincidencia de cargos inválidas.', code='invalid_filters')
        fields = {'person_titles': _strings(titles, 'Cargos'), 'person_locations': _strings(person_locations, 'Ubicación de personas'),
            'organization_locations': _strings(organization_locations, 'Ubicación de empresas'),
            'person_seniorities': _strings(seniorities, 'Nivel de responsabilidad'),
            'organization_num_employees_ranges': _strings(employee_ranges, 'Tamaño de empresa'),
            'q_organization_domains_list': _strings(domains, 'Dominios', 50)}
        if any(value not in SENIORITIES for value in fields['person_seniorities']):
            raise SourceError('El nivel de responsabilidad no está admitido por Apollo.', code='invalid_filters')
        for value in fields['organization_num_employees_ranges']:
            if not re.fullmatch(r'\d{1,8},\d{1,8}', value) or int(value.split(',')[0]) > int(value.split(',')[1]):
                raise SourceError('Usá rangos de empleados como 1,10 o 50,200.', code='invalid_filters')
        fields['q_organization_domains_list'] = [value.lower() for value in fields['q_organization_domains_list']]
        if any(not _domain(value) or value.startswith('www.') for value in fields['q_organization_domains_list']):
            raise SourceError('Usá dominios sin https://, @ ni www.', code='invalid_filters')
        if not keywords.strip() and not any(fields.values()):
            raise SourceError('Definí al menos un filtro antes de buscar.', code='invalid_filters')
        payload = {'page': page, 'per_page': per_page, 'include_similar_titles': include_similar_titles,
            **{name: values for name, values in fields.items() if values}}
        if keywords.strip():
            payload['q_keywords'] = keywords.strip()
        key = self._key(workspace_id, campaign_id)
        data, rates = self._request(workspace_id, campaign_id, SEARCH_PATH, payload, key)
        people = data.get('people')
        if not isinstance(people, list) or len(people) > 100 or any(not isinstance(person, dict) for person in people):
            raise SourceError('Apollo no devolvió una lista válida de personas.', code='invalid_response')
        pagination = data.get('pagination') if isinstance(data.get('pagination'), dict) else {}
        total = data.get('total_entries', pagination.get('total_entries'))
        total = total if type(total) is int and total >= 0 else None
        contacts, seen = [], set()
        for person in people:
            contact = self._normalize(workspace_id, campaign_id, person, enriched=False)
            if contact['provider_id'] not in seen:
                contacts.append(contact)
                seen.add(contact['provider_id'])
        return {'provider': 'apollo', 'workspace_id': workspace_id, 'campaign_id': campaign_id, 'page': page, 'per_page': per_page,
            'total_entries': total, 'has_more': page < 500 and (page * per_page < total if total is not None else len(people) == per_page),
            'contacts': contacts, 'rate_limits': rates,
            'credit_usage': {'may_consume_credits': False, 'documented_credits': 0, 'actual_credits': None,
                'note': 'La búsqueda no revela correos. Obtenerlos requiere un enriquecimiento separado.'}}

    def enrich(self, workspace_id: int, campaign_id: int, person_id: str, *, allow_credit_use: bool = False) -> dict:
        """Explicitly enrich one Apollo ID. Root API owns any subsequent import.

        No request is retried automatically: a timeout can still consume credits.
        Return contact=None with no_match for an explicit unmatched provider result.
        Only importable=True records have a usable address reported as verified;
        verification is Apollo's assertion and is not a deliverability guarantee.
        """
        if allow_credit_use is not True:
            raise SourceError('Habilitá explícitamente el uso de créditos de Apollo para obtener el correo.', code='credit_use_not_authorized')
        if not isinstance(person_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', person_id):
            raise SourceError('Identificador de persona inválido.', code='invalid_person_id')
        key = self._key(workspace_id, campaign_id)
        payload = {'id': person_id, 'reveal_personal_emails': False, 'reveal_phone_number': False,
            'run_waterfall_email': False, 'run_waterfall_phone': False}
        data, rates = self._request(workspace_id, campaign_id, ENRICH_PATH, payload, key)
        if 'person' not in data or (data['person'] is not None and not isinstance(data['person'], dict)):
            raise SourceError('Apollo no devolvió un enriquecimiento válido.', code='invalid_response', credit_outcome_uncertain=True)
        person = data['person']
        contact = self._normalize(workspace_id, campaign_id, person, enriched=True, requested_id=person_id,
            match_confidence=data.get('match_confidence')) if person else None
        return {'provider': 'apollo', 'workspace_id': workspace_id, 'campaign_id': campaign_id,
            'status': contact['status'] if contact else 'no_match', 'contact': contact, 'rate_limits': rates,
            'credit_usage': {'may_consume_credits': True, 'actual_credits': None,
                'note': 'Apollo puede consumir créditos por datos encontrados, incluso si no devuelve un correo. Consultá el consumo real en tu cuenta.'}}
