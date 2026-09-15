import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import stripe
from sqlalchemy import select

from egasis.billing import BillingError, BillingService
from egasis.models import Event, User, WebhookEvent, Workspace
from egasis.store import make_store


@pytest.fixture
def billing(tmp_path):
    engine, factory = make_store('sqlite:///' + str(tmp_path / 'billing.db'))
    with factory.begin() as db:
        one, two = Workspace(name='Uno'), Workspace(name='Dos')
        db.add_all([one, two])
        db.flush()
        db.add_all([User(workspace_id=one.id, email='owner@example.com', password_hash='unused'),
                    User(workspace_id=two.id, email='other@example.com', password_hash='unused'),
                    User(workspace_id=one.id, email='member@example.com', password_hash='unused', role='member')])
    env = {'STRIPE_SECRET_KEY': 'sk_test_PRIVATE', 'STRIPE_WEBHOOK_SECRET': 'whsec_PRIVATE',
           'STRIPE_PRICE_STARTER': 'price_starter', 'STRIPE_PRICE_GROWTH': 'price_growth',
           'STRIPE_PRICE_AGENCY': 'price_agency', 'EGASIS_PUBLIC_URL': 'http://localhost:8765'}
    api = SimpleNamespace(
        Webhook=stripe.Webhook, Customer=Mock(), Subscription=Mock(),
        checkout=SimpleNamespace(Session=Mock()), billing_portal=SimpleNamespace(Session=Mock()),
    )
    api.Customer.create.return_value = {'id': 'cus_one'}
    api.checkout.Session.create.return_value = {'id': 'cs_one', 'url': 'https://checkout.stripe.com/test'}
    api.billing_portal.Session.create.return_value = {'url': 'https://billing.stripe.com/test'}
    api.Subscription.retrieve.return_value = {
        'id': 'sub_one', 'customer': 'cus_one', 'status': 'active',
        'items': {'data': [{'price': {'id': 'price_growth'}}]},
    }
    yield BillingService(factory, env, api), factory, api, env
    engine.dispose()


def deliver(service, env, event_id='evt_one', kind='checkout.session.completed', **updates):
    obj = {'id': 'cs_one', 'customer': 'cus_one', 'subscription': 'sub_one',
           'mode': 'subscription', 'client_reference_id': '1', 'metadata': {}}
    obj.update(updates)
    payload = json.dumps({'id': event_id, 'object': 'event', 'type': kind,
                          'data': {'object': obj}}).encode()
    stamp = int(time.time())
    digest = hmac.new(env['STRIPE_WEBHOOK_SECRET'].encode(), str(stamp).encode() + b'.' + payload, hashlib.sha256).hexdigest()
    return service.webhook(payload, f't={stamp},v1={digest}')


def test_pilot_does_not_require_billing_and_readiness_hides_secrets(billing):
    service, factory, _, _ = billing
    empty = BillingService(factory, {})
    assert empty.readiness()['configured'] is False
    assert 'STRIPE_SECRET_KEY' in empty.readiness()['missing']
    assert 'PRIVATE' not in json.dumps(service.readiness())
    with factory() as db:
        assert db.get(Workspace, 1).plan == 'pilot'
        assert db.get(Workspace, 1).subscription_status == 'pilot'


@pytest.mark.parametrize('user_id', [2, 3, 999])
def test_checkout_revalidates_owner_and_workspace(billing, user_id):
    service, _, api, _ = billing
    with pytest.raises(BillingError) as error:
        service.checkout(1, user_id, 'growth')
    assert error.value.status_code == 403
    api.Customer.create.assert_not_called()


def test_checkout_persists_customer_and_server_session_but_no_paid_state(billing):
    service, factory, api, _ = billing
    assert service.checkout(1, 1, 'growth')['session_id'] == 'cs_one'
    args = api.checkout.Session.create.call_args.kwargs
    assert args['customer'] == 'cus_one'
    assert args['line_items'] == [{'price': 'price_growth', 'quantity': 1}]
    with factory() as db:
        workspace = db.get(Workspace, 1)
        assert workspace.stripe_customer == 'cus_one'
        assert workspace.subscription_status == 'pilot'
        assert workspace.stripe_subscription is None
        assert db.scalar(select(Event).where(Event.kind == 'billing.checkout'))


def test_signed_checkout_reconciles_and_is_durably_idempotent(billing):
    service, factory, api, env = billing
    service.checkout(1, 1, 'growth')
    assert deliver(service, env)['applied'] is True
    # Constructing a fresh service proves this is database-backed, not a cache.
    fresh = BillingService(factory, env, api)
    assert deliver(fresh, env)['duplicate'] is True
    api.Subscription.retrieve.assert_called_once()
    with factory() as db:
        workspace = db.get(Workspace, 1)
        assert (workspace.plan, workspace.subscription_status, workspace.stripe_subscription) == ('growth', 'active', 'sub_one')
    with pytest.raises(BillingError) as error:
        service.checkout(1, 1, 'starter')
    assert error.value.status_code == 409


def test_invalid_signature_never_processes_or_persists(billing):
    service, factory, api, _ = billing
    with pytest.raises(BillingError):
        service.webhook(b'{"id":"evt_forged"}', 't=0,v1=invalid')
    api.Subscription.retrieve.assert_not_called()
    with factory() as db:
        assert db.get(WebhookEvent, 'evt_forged') is None


def test_untrusted_metadata_and_unrecorded_checkout_cannot_bind(billing):
    service, factory, api, env = billing
    service.checkout(1, 1, 'growth')
    assert deliver(service, env, id='cs_unknown', metadata={'workspace_id': '2'})['applied'] is False
    assert deliver(service, env, event_id='evt_two', customer='cus_unknown', metadata={'workspace_id': '1'})['applied'] is False
    api.Subscription.retrieve.assert_not_called()
    with factory() as db:
        assert db.get(Workspace, 1).stripe_subscription is None
        assert db.get(Workspace, 2).stripe_subscription is None


def test_out_of_order_subscription_events_use_current_provider_state(billing):
    service, factory, api, env = billing
    service.checkout(1, 1, 'growth')
    assert deliver(service, env, 'evt_early', 'customer.subscription.created', id='sub_one')['applied'] is False
    deliver(service, env)
    api.Subscription.retrieve.return_value['status'] = 'canceled'
    # The old payload says active; reconciliation must preserve cancellation.
    deliver(service, env, 'evt_stale', 'customer.subscription.updated', id='sub_one', status='active')
    assert deliver(service, env, 'evt_unrelated', 'customer.subscription.created', id='sub_other')['applied'] is False
    with factory() as db:
        assert db.get(Workspace, 1).subscription_status == 'canceled'
        assert db.get(Workspace, 1).stripe_subscription == 'sub_one'


def test_provider_failure_leaves_event_retryable(billing):
    service, factory, api, env = billing
    service.checkout(1, 1, 'growth')
    api.Subscription.retrieve.side_effect = RuntimeError('temporary provider problem')
    with pytest.raises(RuntimeError):
        deliver(service, env)
    with factory() as db:
        assert db.get(WebhookEvent, 'evt_one') is None
        assert db.get(Workspace, 1).stripe_subscription is None
    api.Subscription.retrieve.side_effect = None
    assert deliver(service, env)['applied'] is True


def test_customer_mismatch_is_rejected_without_binding(billing):
    service, factory, api, env = billing
    service.checkout(1, 1, 'growth')
    api.Subscription.retrieve.return_value['customer'] = 'cus_someone_else'
    with pytest.raises(BillingError):
        deliver(service, env)
    with factory() as db:
        assert db.get(Workspace, 1).stripe_subscription is None
        assert db.get(WebhookEvent, 'evt_one') is None


def test_portal_uses_bound_customer_and_checks_owner(billing):
    service, _, api, _ = billing
    service.checkout(1, 1, 'growth')
    assert service.portal(1, 1)['url'].startswith('https://billing.stripe.com/')
    assert api.billing_portal.Session.create.call_args.kwargs['customer'] == 'cus_one'
    with pytest.raises(BillingError):
        service.portal(1, 2)


def test_remote_cleartext_origin_rejected(billing):
    service, _, api, env = billing
    env['EGASIS_PUBLIC_URL'] = 'http://example.com'
    with pytest.raises(BillingError):
        service.checkout(1, 1, 'growth')
    api.Customer.create.assert_not_called()
