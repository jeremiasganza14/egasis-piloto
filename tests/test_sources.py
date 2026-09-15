import json

import httpx
import pytest
from sqlalchemy import func, select

from egasis.models import Campaign, Connection, Contact, Event, Suppression, Workspace
from egasis.prospect_sources import ApolloSource, ENRICH_PATH, SEARCH_PATH, SourceError, SourceRateLimited, SourceUnavailable
from egasis.security import Vault
from egasis.store import make_store


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Only httpx.MockTransport is permitted in source tests')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.delenv('EGASIS_APOLLO_INTEGRATION_AUTHORIZED', raising=False)


@pytest.fixture
def source_setup(tmp_path, monkeypatch):
    engine, sessions = make_store('sqlite:///' + str(tmp_path / 'sources.sqlite'))
    vault = Vault('isolated-source-test-key')
    with sessions() as db:
        first, second = Workspace(name='Primera empresa'), Workspace(name='Segunda empresa')
        db.add_all((first, second))
        db.flush()
        campaign = Campaign(workspace_id=first.id, name='Servicios industriales', subject='Una propuesta', body='Nuestra oferta')
        other = Campaign(workspace_id=second.id, name='Otros servicios', subject='Privado', body='Privado')
        db.add_all((campaign, other))
        db.add_all((Connection(workspace_id=first.id, provider='apollo', secret=vault.encrypt('first-workspace-key')),
                    Connection(workspace_id=second.id, provider='apollo', secret=vault.encrypt('second-workspace-key'))))
        db.commit()
        ids = first.id, campaign.id, second.id, other.id
    monkeypatch.setenv('EGASIS_APOLLO_INTEGRATION_AUTHORIZED', 'true')
    yield sessions, vault, ids
    engine.dispose()


def source(setup, handler):
    sessions, vault, _ = setup
    return ApolloSource(sessions, vault, httpx.MockTransport(handler))


def search_person(**overrides):
    return {'id': 'person-one', 'first_name': 'Ana', 'last_name_obfuscated': 'Pé***z', 'title': 'Gerente de compras',
        'has_email': True, 'organization': {'name': 'Industria del Sur', 'has_city': True}, **overrides}


def enriched_person(**overrides):
    return {'id': 'person-one', 'name': 'Ana Pérez', 'first_name': 'Ana', 'last_name': 'Pérez', 'title': 'Gerente de compras',
        'email': 'ana@buyer-business.com', 'email_status': 'verified',
        'organization': {'name': 'Industria del Sur', 'primary_domain': 'buyer-business.com'}, **overrides}


@pytest.mark.parametrize('flag', ['', 'false', '1', 'yes'])
def test_integration_needs_explicit_true_despite_valid_workspace_key(source_setup, monkeypatch, flag):
    monkeypatch.setenv('EGASIS_APOLLO_INTEGRATION_AUTHORIZED', flag)
    connector = source(source_setup, lambda request: pytest.fail('Unauthorized integration made a request'))
    wid, cid, _, _ = source_setup[2]
    assert connector.readiness(wid)['available'] is False
    with pytest.raises(SourceUnavailable) as error:
        connector.search(wid, cid, keywords='manufactura')
    assert error.value.code == 'integration_not_authorized'


def test_search_uses_official_payload_own_key_and_no_email_even_if_unexpectedly_returned(source_setup):
    sessions, _, (wid, cid, _, _) = source_setup
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'POST' and request.url.path == SEARCH_PATH
        assert request.headers['x-api-key'] == 'first-workspace-key'
        data = json.loads(request.content)
        assert data == {'page': 2, 'per_page': 10, 'include_similar_titles': False, 'q_keywords': 'logística',
            'person_titles': ['Compras'], 'person_locations': ['Argentina'], 'organization_locations': ['Uruguay'],
            'person_seniorities': ['director'], 'organization_num_employees_ranges': ['50,200'], 'q_organization_domains_list': ['buyer-business.com']}
        return httpx.Response(200, json={'total_entries': 250, 'people': [search_person(email='unexpected@buyer-business.com')]}, headers={'x-minute-requests-left': '49'})
    preview = source(source_setup, handler).search(wid, cid, keywords='logística', titles=['Compras'],
        person_locations=['Argentina'], organization_locations=['Uruguay'], seniorities=['director'],
        employee_ranges=['50,200'], domains=['buyer-business.com'], page=2, per_page=10)
    row = preview['contacts'][0]
    assert row['email'] is None and row['status'] == 'preview_no_email' and row['importable'] is False
    assert row['name'] == 'Ana' and row['display_name'] == 'Ana Pé***z'
    assert row['company'] == 'Industria del Sur' and row['has_email'] is True
    assert row['source_metadata']['email_revealed'] is False
    assert preview['has_more'] and preview['rate_limits']['x-minute-requests-left'] == 49
    assert preview['credit_usage']['may_consume_credits'] is False
    assert len(calls) == 1
    with sessions() as db:
        assert db.scalar(select(func.count(Contact.id))) == 0
        assert db.scalar(select(func.count(Event.id))) == 2


def test_no_shared_key_fallback_and_no_cross_workspace_campaign(source_setup, monkeypatch):
    sessions, _, (wid, cid, other_wid, other_cid) = source_setup
    connector = source(source_setup, lambda request: pytest.fail('Wrong tenant request'))
    with pytest.raises(SourceError) as error:
        connector.search(wid, other_cid, titles=['Owner'])
    assert error.value.code == 'campaign_not_found'
    with sessions() as db:
        db.delete(db.scalar(select(Connection).where(Connection.workspace_id == wid)))
        db.commit()
    monkeypatch.setenv('APOLLO_API_KEY', 'forbidden-shared-key')
    monkeypatch.setenv('EGASIS_APOLLO_API_KEY', 'forbidden-shared-key')
    with pytest.raises(SourceUnavailable) as error:
        connector.search(wid, cid, titles=['Owner'])
    assert error.value.code == 'key_missing'
    assert connector.readiness(wid)['key_configured'] is False
    assert connector.readiness(other_wid)['key_configured'] is True


def test_two_workspaces_really_use_their_own_keys(source_setup):
    _, _, (wid, cid, other_wid, other_cid) = source_setup
    seen = []
    def handler(request):
        seen.append(request.headers['x-api-key'])
        return httpx.Response(200, json={'total_entries': 0, 'people': []})
    connector = source(source_setup, handler)
    connector.search(wid, cid, keywords='consultoría')
    connector.search(other_wid, other_cid, keywords='diseño')
    assert seen == ['first-workspace-key', 'second-workspace-key']


def test_enrichment_requires_explicit_credits_and_disables_phone_personal_waterfall(source_setup):
    sessions, _, (wid, cid, _, _) = source_setup
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path == ENRICH_PATH
        assert json.loads(request.content) == {'id': 'person-one', 'reveal_personal_emails': False,
            'reveal_phone_number': False, 'run_waterfall_email': False, 'run_waterfall_phone': False}
        return httpx.Response(200, json={'person': enriched_person(), 'match_confidence': 'high'})
    connector = source(source_setup, handler)
    with pytest.raises(SourceError) as error:
        connector.enrich(wid, cid, 'person-one')
    assert error.value.code == 'credit_use_not_authorized' and not calls
    enriched = connector.enrich(wid, cid, 'person-one', allow_credit_use=True)
    assert enriched['contact']['email'] == 'ana@buyer-business.com'
    assert enriched['contact']['status'] == 'ready' and enriched['contact']['importable']
    assert enriched['contact']['website'] == 'https://buyer-business.com'
    assert enriched['credit_usage']['actual_credits'] is None
    assert len(calls) == 1
    with sessions() as db:
        assert db.scalar(select(func.count(Contact.id))) == 0


@pytest.mark.parametrize('email,status,expected', [
    (None, 'unavailable', 'no_email'), ('email_not_unlocked@domain.com', 'verified', 'no_email'),
    ('[email protected]', 'verified', 'no_email'), ('ana@example.com', 'verified', 'no_email'),
    ('ana@buyer-business.com', 'unverified', 'review_email'), ('ana@buyer-business.com', 'extrapolated', 'review_email')])
def test_nonusable_email_is_never_importable(source_setup, email, status, expected):
    wid, cid, _, _ = source_setup[2]
    connector = source(source_setup, lambda request: httpx.Response(200, json={'person': enriched_person(email=email, email_status=status)}))
    result = connector.enrich(wid, cid, 'person-one', allow_credit_use=True)
    assert result['contact']['status'] == expected and not result['contact']['importable']


def test_suppression_and_duplicates_are_workspace_scoped(source_setup):
    sessions, _, (wid, cid, other_wid, other_cid) = source_setup
    connector = source(source_setup, lambda request: httpx.Response(200, json={'person': enriched_person()}))
    with sessions() as db:
        db.add(Contact(workspace_id=other_wid, campaign_id=other_cid, email='ana@buyer-business.com'))
        db.add(Suppression(workspace_id=other_wid, email='ana@buyer-business.com', reason='unsubscribe'))
        db.commit()
    assert connector.enrich(wid, cid, 'person-one', allow_credit_use=True)['contact']['status'] == 'ready'
    with sessions() as db:
        db.add(Contact(workspace_id=wid, campaign_id=cid, email='ana@buyer-business.com'))
        db.commit()
    assert connector.enrich(wid, cid, 'person-one', allow_credit_use=True)['contact']['status'] == 'duplicate'
    with sessions() as db:
        db.add(Suppression(workspace_id=wid, email='ana@buyer-business.com', reason='unsubscribe'))
        db.commit()
    assert connector.enrich(wid, cid, 'person-one', allow_credit_use=True)['contact']['status'] == 'suppressed'


@pytest.mark.parametrize('status,code', [(401, 'authentication_failed'), (403, 'access_denied'), (422, 'invalid_provider_request'), (500, 'provider_unavailable'), (302, 'provider_unavailable')])
def test_provider_errors_do_not_leak_response_bodies_or_retry(source_setup, status, code):
    sessions, _, (wid, cid, _, _) = source_setup
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='SECRET first-workspace-key', headers={'Location': 'https://other-host.com'})
    with pytest.raises(SourceError) as error:
        source(source_setup, handler).search(wid, cid, keywords='servicios')
    assert error.value.code == code and 'SECRET' not in str(error.value)
    assert len(calls) == 1
    with sessions() as db:
        assert all('first-workspace-key' not in event.detail for event in db.scalars(select(Event)))


def test_rate_limit_exposes_provider_wait_without_guessing_or_retry(source_setup):
    wid, cid, _, _ = source_setup[2]
    connector = source(source_setup, lambda request: httpx.Response(429, headers={'Retry-After': '3599'}, json={'error': 'quota'}))
    with pytest.raises(SourceRateLimited) as error:
        connector.search(wid, cid, keywords='servicios')
    assert error.value.retry_after_seconds == 3599 and error.value.http_status == 429


def test_enrichment_timeout_explicitly_reports_possible_credit_use(source_setup):
    wid, cid, _, _ = source_setup[2]
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('SECRET first-workspace-key')
    with pytest.raises(SourceUnavailable) as error:
        source(source_setup, handler).enrich(wid, cid, 'person-one', allow_credit_use=True)
    assert error.value.credit_outcome_uncertain
    assert 'SECRET' not in str(error.value) and len(calls) == 1


@pytest.mark.parametrize('filters', [{}, {'page': 501, 'titles': ['Owner']}, {'per_page': 101, 'keywords': 'x'},
    {'titles': 'Owner'}, {'employee_ranges': ['200,10']}, {'domains': ['https://business.com']},
    {'seniorities': ['made_up']}, {'keywords': 'x', 'include_similar_titles': 'true'}])
def test_invalid_filters_fail_before_network(source_setup, filters):
    wid, cid, _, _ = source_setup[2]
    connector = source(source_setup, lambda request: pytest.fail('Invalid filter request'))
    with pytest.raises(SourceError) as error:
        connector.search(wid, cid, **filters)
    assert error.value.code == 'invalid_filters'


def test_no_match_duplicate_ids_and_malformed_responses_have_no_fake_fallback(source_setup):
    wid, cid, _, _ = source_setup[2]
    connector = source(source_setup, lambda request: httpx.Response(200, json={'person': None}))
    assert connector.enrich(wid, cid, 'person-one', allow_credit_use=True)['status'] == 'no_match'
    connector = source(source_setup, lambda request: httpx.Response(200, json={'people': [search_person(), search_person()], 'total_entries': 1}))
    assert len(connector.search(wid, cid, keywords='servicios')['contacts']) == 1
    for payload in ({'people': 'wrong'}, {'people': [{}]}, {'error': 'SECRET'}, []):
        connector = source(source_setup, lambda request: httpx.Response(200, json=payload))
        with pytest.raises(SourceError):
            connector.search(wid, cid, keywords='servicios')


def test_enrichment_rejects_wrong_person_id_and_holds_low_confidence_for_review(source_setup):
    wid, cid, _, _ = source_setup[2]
    connector = source(source_setup, lambda request: httpx.Response(200, json={'person': enriched_person(id='someone-else')}))
    with pytest.raises(SourceError) as error:
        connector.enrich(wid, cid, 'person-one', allow_credit_use=True)
    assert error.value.code == 'identity_mismatch'
    connector = source(source_setup, lambda request: httpx.Response(200, json={'person': enriched_person(), 'match_confidence': 'low'}))
    assert connector.enrich(wid, cid, 'person-one', allow_credit_use=True)['contact']['status'] == 'review_email'


def test_provider_website_cannot_introduce_private_or_unsafe_url(source_setup):
    wid, cid, _, _ = source_setup[2]
    for website in ('http://127.0.0.1/secrets', 'http://localhost/', 'javascript:alert(1)', 'https://user:password@business.com/'):
        person = enriched_person(organization={'name': 'Empresa', 'website_url': website})
        connector = source(source_setup, lambda request: httpx.Response(200, json={'person': person}))
        assert connector.enrich(wid, cid, 'person-one', allow_credit_use=True)['contact']['website'] == ''
