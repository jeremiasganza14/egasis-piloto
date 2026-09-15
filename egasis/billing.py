"""Optional Stripe billing. The pilot never depends on payment configuration.

Webhook bodies must arrive as the original bytes. Only a Checkout session created
here can bind a subscription; event metadata is never an authority for tenancy.
"""
import hashlib
import json
import os
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

import stripe
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from .models import Event, User, WebhookEvent, Workspace


PLANS = {
    'starter': {'name': 'Starter', 'monthly_usd': 299, 'price_env': 'STRIPE_PRICE_STARTER'},
    'growth': {'name': 'Growth', 'monthly_usd': 799, 'price_env': 'STRIPE_PRICE_GROWTH'},
    'agency': {'name': 'Agency', 'monthly_usd': 1299, 'price_env': 'STRIPE_PRICE_AGENCY'},
}
TERMINAL_STATUSES = {'canceled', 'incomplete_expired'}


class BillingError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _id(value):
    return value.get('id') if isinstance(value, dict) else value


def _plain(value):
    return value.to_dict() if hasattr(value, 'to_dict') else value


class BillingService:
    def __init__(self, factory, env=None, stripe_module=None):
        self.factory = factory
        self.env = os.environ if env is None else env
        self.stripe = stripe if stripe_module is None else stripe_module

    def readiness(self):
        required = ['STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET', 'EGASIS_PUBLIC_URL']
        required += [p['price_env'] for p in PLANS.values()]
        missing = [key for key in required if not self.env.get(key, '').strip()]
        return {
            'configured': not missing,
            'missing': missing,
            'plans': [dict(id=tier, name=p['name'], monthly_usd=p['monthly_usd'])
                      for tier, p in PLANS.items()],
        }

    def _key(self):
        key = self.env.get('STRIPE_SECRET_KEY', '').strip()
        if not key:
            raise BillingError('Falta configurar STRIPE_SECRET_KEY.', 503)
        return key

    def _origin(self):
        value = self.env.get('EGASIS_PUBLIC_URL', '').strip().rstrip('/')
        parsed = urlsplit(value)
        local = parsed.hostname in {'localhost', '127.0.0.1', '::1'}
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in {'', '/'}
                or (parsed.scheme != 'https' and not (local and parsed.scheme == 'http'))):
            raise BillingError('EGASIS_PUBLIC_URL debe ser un origen HTTPS válido (HTTP solo local).', 503)
        return value

    @contextmanager
    def _transaction(self):
        with self.factory() as db:
            # SQLite has no row locks. Hold the write reservation while reconciling
            # so concurrent webhook retries cannot both apply the same event.
            if db.bind.dialect.name == 'sqlite':
                db.execute(text('BEGIN IMMEDIATE'))
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _owner(self, db, workspace_id, user_id):
        user = db.get(User, user_id)
        if not user or user.workspace_id != workspace_id or user.role != 'owner':
            raise BillingError('Solo el propietario puede gestionar la suscripción.', 403)
        workspace = db.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update())
        if not workspace:
            raise BillingError('Espacio de trabajo inexistente.', 404)
        return workspace, user

    def checkout(self, workspace_id, user_id, tier):
        if tier not in PLANS:
            raise BillingError('Plan desconocido.')
        readiness = self.readiness()
        if not readiness['configured']:
            raise BillingError('Facturación pendiente: ' + ', '.join(readiness['missing']), 503)
        origin, key = self._origin(), self._key()
        price_id = self.env[PLANS[tier]['price_env']].strip()
        with self._transaction() as db:
            workspace, user = self._owner(db, workspace_id, user_id)
            if workspace.stripe_subscription and workspace.subscription_status not in TERMINAL_STATUSES:
                raise BillingError('Ya existe una suscripción; usá el portal para modificarla.', 409)
            namespace = hashlib.sha256(f'{origin}:{workspace.id}:{workspace.created_at}'.encode()).hexdigest()[:32]
            if not workspace.stripe_customer:
                customer = self.stripe.Customer.create(
                    email=user.email, name=workspace.name, api_key=key,
                    idempotency_key=f'egasis-customer-{namespace}',
                )
                workspace.stripe_customer = customer['id']
                db.flush()
            # A rapid retry uses the same Checkout resource. A saved record, not
            # caller-controlled metadata, authenticates the subsequent binding.
            request_key = f'egasis-checkout-{namespace}-{tier}-{int(time.time() // 1800)}'
            session = self.stripe.checkout.Session.create(
                mode='subscription', customer=workspace.stripe_customer,
                line_items=[{'price': price_id, 'quantity': 1}],
                success_url=origin + '/?billing=success', cancel_url=origin + '/?billing=canceled',
                client_reference_id=str(workspace.id), api_key=key, idempotency_key=request_key,
            )
            record = json.dumps({'session_id': session['id'], 'customer': workspace.stripe_customer,
                                 'tier': tier}, sort_keys=True)
            if not db.scalar(select(Event.id).where(Event.workspace_id == workspace.id,
                                                    Event.kind == 'billing.checkout', Event.detail == record)):
                db.add(Event(workspace_id=workspace.id, kind='billing.checkout', detail=record))
            return {'url': session['url'], 'session_id': session['id']}

    def portal(self, workspace_id, user_id):
        with self._transaction() as db:
            workspace, _ = self._owner(db, workspace_id, user_id)
            if not workspace.stripe_customer:
                raise BillingError('Este espacio todavía no tiene un cliente de facturación.', 409)
            session = self.stripe.billing_portal.Session.create(
                customer=workspace.stripe_customer, return_url=self._origin(), api_key=self._key(),
            )
            return {'url': session['url']}

    def _reconcile(self, workspace, subscription_id):
        # Stripe does not guarantee event order. Fetch current provider state
        # instead of copying a potentially stale event payload into the account.
        subscription = _plain(self.stripe.Subscription.retrieve(subscription_id, api_key=self._key()))
        if _id(subscription.get('customer')) != workspace.stripe_customer:
            raise BillingError('La suscripción no corresponde al cliente registrado.', 400)
        if subscription.get('id') != subscription_id:
            raise BillingError('Respuesta de suscripción inconsistente.', 502)
        prices = [_id(item.get('price')) for item in subscription.get('items', {}).get('data', [])]
        tiers = [tier for tier, plan in PLANS.items()
                 if self.env.get(plan['price_env']) and self.env[plan['price_env']] in prices]
        if len(tiers) != 1 or len(prices) != 1:
            raise BillingError('El precio de la suscripción no coincide con un plan configurado.', 409)
        workspace.stripe_subscription = subscription_id
        workspace.plan = tiers[0]
        workspace.subscription_status = subscription.get('status', 'incomplete')

    def webhook(self, payload, signature):
        secret = self.env.get('STRIPE_WEBHOOK_SECRET', '').strip()
        if not secret:
            raise BillingError('Falta configurar STRIPE_WEBHOOK_SECRET.', 503)
        try:
            event = _plain(self.stripe.Webhook.construct_event(payload, signature, secret))
        except (ValueError, stripe.SignatureVerificationError) as exc:
            raise BillingError('Firma de webhook inválida.', 400) from exc
        event_id = event.get('id')
        if not event_id:
            raise BillingError('Evento de Stripe sin identificador.', 400)
        with self._transaction() as db:
            if db.get(WebhookEvent, event_id):
                return {'received': True, 'duplicate': True}
            try:
                with db.begin_nested():
                    db.add(WebhookEvent(id=event_id))
                    db.flush()
            except IntegrityError:
                # Concurrent delivery: the other transaction committed the same
                # durable event ID. Failed processing rolls this marker back.
                return {'received': True, 'duplicate': True}
            obj = event.get('data', {}).get('object', {})
            customer = _id(obj.get('customer'))
            workspace = db.scalar(select(Workspace).where(Workspace.stripe_customer == customer).with_for_update()) if customer else None
            applied = False
            kind = event.get('type', '')
            if workspace and kind == 'checkout.session.completed' and obj.get('mode') == 'subscription':
                checkouts = db.scalars(select(Event).where(Event.workspace_id == workspace.id,
                                                           Event.kind == 'billing.checkout').order_by(Event.id.desc())).all()
                if checkouts:
                    checkout = json.loads(checkouts[0].detail)
                    if (checkout.get('session_id') == obj.get('id')
                            and checkout.get('customer') == customer
                            and obj.get('client_reference_id') == str(workspace.id)):
                        sub_id = _id(obj.get('subscription'))
                        if not sub_id:
                            raise BillingError('Checkout sin suscripción; Stripe debe reintentar.', 503)
                        self._reconcile(workspace, sub_id)
                        applied = True
            elif workspace and kind.startswith('customer.subscription.'):
                if obj.get('id') == workspace.stripe_subscription:
                    self._reconcile(workspace, workspace.stripe_subscription)
                    applied = True
            if applied:
                db.add(Event(workspace_id=workspace.id, kind='billing.subscription', detail=json.dumps({
                    'event_id': event_id, 'subscription': workspace.stripe_subscription,
                    'plan': workspace.plan, 'status': workspace.subscription_status,
                }, sort_keys=True)))
            return {'received': True, 'duplicate': False, 'applied': applied}
