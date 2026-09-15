"""Workspace-scoped outbox, SMTP delivery, and incremental IMAP ingestion.

An SMTP acknowledgement means accepted by the submitting server, not delivered.
A lost acknowledgement is deliberately held for reconciliation, never retried.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, parseaddr
from hashlib import sha256
from html.parser import HTMLParser
import imaplib
import json
import math
import re
import smtplib
import ssl
import time
from urllib.parse import urlencode, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from .models import Account, Campaign, Contact, Event, Job, Message, Suppression, Workspace


class DeliveryUncertain(Exception):
    """The SMTP DATA acknowledgement was lost; a retry could duplicate mail."""


class DeliveryRejected(Exception):
    def __init__(self, code, stage):
        self.code = code
        super().__init__(f'SMTP {code} during {stage}')


class _PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div', 'li', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'div', 'li', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _plain_body(message):
    plain, html, delivery = [], [], []
    for part in message.walk():
        if part.get_content_disposition() == 'attachment':
            continue
        content_type = part.get_content_type()
        if content_type == 'message/delivery-status':
            for block in part.get_payload() or []:
                delivery.append(block.as_string())
        if content_type not in ('text/plain', 'text/html'):
            continue
        try:
            value = part.get_content()
        except (LookupError, UnicodeError):
            value = (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
        if not isinstance(value, str):
            continue
        if content_type == 'text/plain':
            plain.append(value)
        else:
            parser = _PlainHTML()
            parser.feed(value)
            html.append(''.join(parser.parts))
    return '\n'.join((plain or html) + delivery).strip()[:200_000]


def _identity(value):
    value = (value or '').strip()
    return value if len(value) <= 255 else 'sha256:' + sha256(value.encode()).hexdigest()


def _references(value):
    ids = re.findall(r'<[^<>\s]+>', value or '')
    return ids or ([value.strip()] if value and value.strip() else [])


def _is_demo(contact):
    domain = contact.email.lower().rsplit('@', 1)[-1]
    return contact.source == 'demo' or domain in {'example.com', 'example.org', 'example.net', 'localhost'} or domain.endswith(('.example', '.invalid', '.test', '.localhost', '.example.com', '.example.org', '.example.net'))


def _unsubscribe_requested(body):
    # Only examine the sender's new text, never an opt-out phrase in quoted mail.
    lines = []
    for line in body.splitlines():
        if line.lstrip().startswith('>') or re.match(r'^(On .+wrote:|El .+escribi[oó]:|[-_]{3,}|From:|De:)', line.strip(), re.I):
            break
        lines.append(line)
    new_text = '\n'.join(lines).strip().lower()
    if re.fullmatch(r'((please\s+)?unsubscribe(\s+me)?|baja|stop|cancelar suscripci[oó]n)[.!\s]*', new_text):
        return True
    phrases = (
        r'\bremove me from (your|the) (list|mailing)',
        r'\bstop (emailing|contacting|messaging) me\b',
        r'\bdo not (email|contact|message) me\b',
        r'\bdon[’\x27]t (email|contact|message) me\b',
        r'\bno me (contacte[sn]?|escriba[sn]?|env[ií]e[sn]? (m[aá]s )?(correos|emails|mails))\b',
        r'\b(dame|darme|denme|quiero darme) de baja\b',
        r'\b(elim[ií]name|eliminenme|qu[ií]tame) de (su|tu|la) lista\b',
    )
    return any(re.search(pattern, new_text) for pattern in phrases)


class MailEngine:
    LEASE_SECONDS = 300
    MAX_ATTEMPTS = 3
    SENDING_SUBSCRIPTIONS = frozenset(('pilot', 'active', 'trialing'))

    def __init__(self, session_factory, vault, simulation=True, public_url='http://127.0.0.1:8765'):
        self.sessions = session_factory
        self.vault = vault
        self.simulation = simulation
        self.public_url = public_url.rstrip('/')
        self.clock = time.time
        self.imap_factory = imaplib.IMAP4_SSL

    @contextmanager
    def _transaction(self, workspace_id=None):
        with self.sessions() as db:
            try:
                if db.bind.dialect.name == 'sqlite':
                    db.execute(text('BEGIN IMMEDIATE'))
                elif workspace_id is not None:
                    db.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _event(db, workspace_id, kind, detail):
        db.add(Event(workspace_id=workspace_id, kind=kind, detail=detail))

    @staticmethod
    def _cancel(db, job, message, reason):
        job.status, job.error = 'cancelled', reason
        message.status, message.error = 'cancelled', reason

    @staticmethod
    def _current_conversation(db, message):
        """Never send a prepared response that predates a newer inbound message."""
        key = message.idempotency_key or ''
        latest = db.scalar(select(Message).where(Message.workspace_id == message.workspace_id,
            Message.contact_id == message.contact_id, Message.direction == 'inbound')
            .order_by(Message.created_at.desc(), Message.id.desc()).limit(1))
        if key.startswith(('first:', 'followup:')):
            return latest is None
        if key.startswith('reply:'):
            try:
                target = int(key.removeprefix('reply:'))
            except ValueError:
                return False
            return bool(latest and latest.id == target and latest.account_id == message.account_id
                and (latest.provider_id or '') == (message.in_reply_to or ''))
        if message.in_reply_to:
            return bool(latest and latest.account_id == message.account_id and latest.provider_id == message.in_reply_to)
        return latest is None

    def prepare_campaign(self, workspace_id, campaign_id):
        """Render the configured first message once per contact, without AI edits."""
        with self._transaction(workspace_id) as db:
            workspace = db.get(Workspace, workspace_id)
            campaign = db.scalar(select(Campaign).where(Campaign.id == campaign_id, Campaign.workspace_id == workspace_id))
            if not workspace or not campaign:
                raise ValueError('Campaign does not belong to workspace')
            accounts = list(db.scalars(select(Account).where(Account.workspace_id == workspace_id, Account.active.is_(True)).order_by(Account.id)))
            if not accounts:
                raise ValueError('An active email account is required')
            account_ids = {account.id for account in accounts}
            suppressed = {value.lower() for value in db.scalars(select(Suppression.email).where(Suppression.workspace_id == workspace_id))}
            contacts = list(db.scalars(select(Contact).where(Contact.workspace_id == workspace_id, Contact.campaign_id == campaign_id, Contact.status == 'pending').order_by(Contact.id)))
            created = 0
            for contact in contacts:
                if contact.email.lower() in suppressed:
                    contact.status = 'do_not_contact'
                    continue
                if not self.simulation and _is_demo(contact):
                    continue
                key = f'first:{campaign_id}:{contact.id}'
                existing = db.scalar(select(Message).where(Message.workspace_id == workspace_id, Message.idempotency_key == key))
                old_job = db.scalar(select(Job).where(Job.message_id == existing.id)) if existing else None
                if existing and not (existing.status == 'cancelled' and old_job and old_job.status == 'cancelled'
                                     and old_job.workspace_id == workspace_id and old_job.attempts == 0
                                     and not old_job.reserved_at and not old_job.lease_until
                                     and existing.direction == 'outbound' and existing.contact_id == contact.id
                                     and existing.account_id == contact.account_id and not existing.provider_id and existing.sent_at is None
                                     and not db.scalar(select(Message.id).where(Message.workspace_id == workspace_id,
                                         Message.contact_id == contact.id,Message.direction == 'inbound'))):
                    continue
                if contact.account_id and contact.account_id not in account_ids:
                    continue  # Never silently move an existing conversation to another sender.
                contact.account_id = contact.account_id or accounts[created % len(accounts)].id
                values = {'name': contact.name or '', 'company': contact.company or '', 'offer': campaign.offer or workspace.offer or '', 'signature': workspace.signature or ''}
                values['booking_link'] = self.public_url + '/book?' + urlencode({'workspace_id':workspace_id,'contact_id':contact.id,'token':self.vault.booking_token(workspace_id,contact.id)})
                def render(template):
                    unknown = set(re.findall(r'\{\{\s*(\w+)\s*\}\}', template)) - values.keys()
                    if unknown:
                        raise ValueError('Unsupported template fields: ' + ', '.join(sorted(unknown)))
                    return re.sub(r'\{\{\s*(\w+)\s*\}\}', lambda match: values[match.group(1)], template)
                subject, body = render(campaign.subject), render(campaign.body)
                if '\n' in subject or '\r' in subject or not subject.strip() or not body.strip():
                    raise ValueError('A nonempty subject and body without header line breaks are required')
                if existing:
                    existing.subject,existing.body,existing.status,existing.error=subject,body,'queued',''
                    old_job.status,old_job.error,old_job.due_at='pending','',self.clock()
                else:
                    message = Message(workspace_id=workspace_id, contact_id=contact.id, account_id=contact.account_id, direction='outbound', subject=subject, body=body, status='queued', idempotency_key=key)
                    db.add(message)
                    db.flush()
                    db.add(Job(workspace_id=workspace_id, message_id=message.id, due_at=self.clock()))
                created += 1
            if created:
                self._event(db, workspace_id, 'campaign.prepared', f'Campaign {campaign_id}: {created} messages queued')
            return created

    @staticmethod
    def _in_window(campaign, workspace, now):
        local = datetime.fromtimestamp(now, ZoneInfo(workspace.timezone))
        weekdays = {int(value.strip()) for value in campaign.weekdays.split(',') if value.strip()}
        start, end = campaign.start_hour, campaign.end_hour
        if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
            return False
        if start < end:
            return local.weekday() in weekdays and start <= local.hour < end
        if local.hour >= start:
            return local.weekday() in weekdays
        return local.hour < end and (local - timedelta(days=1)).weekday() in weekdays

    def _claim(self, job_id, workspace_id, now):
        with self._transaction(workspace_id) as db:
            job = db.scalar(select(Job).where(Job.id == job_id, Job.workspace_id == workspace_id).with_for_update())
            if not job or job.status != 'pending' or job.due_at > now:
                return None
            message = db.get(Message, job.message_id)
            contact = db.get(Contact, message.contact_id) if message else None
            account = db.get(Account, message.account_id) if message and message.account_id else None
            campaign = db.get(Campaign, contact.campaign_id) if contact else None
            workspace = db.get(Workspace, workspace_id)
            if not all((message, contact, account, campaign, workspace)) or any(obj.workspace_id != workspace_id for obj in (message, contact, account, campaign)) or contact.account_id != account.id:
                if message:
                    self._cancel(db, job, message, 'Conversation sender or workspace mismatch')
                else:
                    job.status, job.error = 'cancelled', 'Missing message'
                return None
            if message.direction != 'outbound' or message.status not in ('queued', 'pending', 'failed'):
                self._cancel(db, job, message, 'Message is not eligible for delivery')
                return None
            if not self._current_conversation(db, message):
                self._cancel(db, job, message, 'A newer inbound message requires a fresh response')
                self._event(db, workspace_id, 'mail.stale_cancelled', f'Message {message.id}: conversation changed before claim')
                return None
            if db.scalar(select(Suppression.id).where(Suppression.workspace_id == workspace_id, func.lower(Suppression.email) == contact.email.lower())) or contact.status in ('do_not_contact', 'bounced'):
                self._cancel(db, job, message, 'Recipient is suppressed')
                return None
            if (message.idempotency_key or '').startswith(('first:', 'followup:')) and contact.status in ('replied', 'interested', 'not_interested'):
                self._cancel(db, job, message, 'Recipient has already replied')
                return None
            if not self.simulation and _is_demo(contact):
                self._cancel(db, job, message, 'Example contacts cannot receive real email')
                return None
            if campaign.status != 'active' or not account.active:
                return None
            if workspace.subscription_status not in self.SENDING_SUBSCRIPTIONS:
                job.error = 'Subscription does not allow sending'
                return None
            try:
                if not self._in_window(campaign, workspace, now):
                    return None
                zone = ZoneInfo(workspace.timezone)
                day_start = datetime.fromtimestamp(now, zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            except (ValueError, KeyError):
                job.error = 'Invalid sending schedule or timezone'
                return None
            if now - (account.last_sent_at or 0) < max(0, account.cooldown_seconds):
                return None
            counted = ('sending', 'sent', 'simulated', 'uncertain')
            quota = select(func.count(Message.id)).join(Job, Job.message_id == Message.id).where(Message.workspace_id == workspace_id, Message.status.in_(counted), Job.reserved_at >= day_start)
            account_count = db.scalar(quota.where(Message.account_id == account.id))
            campaign_count = db.scalar(quota.join(Contact, Contact.id == Message.contact_id).where(Contact.campaign_id == campaign.id))
            if account_count >= account.daily_limit or campaign_count >= campaign.daily_limit:
                return None
            job.status, job.attempts = 'running', job.attempts + 1
            job.reserved_at, job.lease_until = now, now + self.LEASE_SECONDS
            job.error = ''
            message.status, message.error = 'sending', ''
            domain = account.email.rsplit('@', 1)[-1]
            message.provider_id = message.provider_id or f'<{uuid4().hex}.egasis@{domain}>'
            account.last_sent_at = now
            references = list(db.scalars(select(Message.provider_id).where(Message.contact_id == contact.id, Message.account_id == account.id, Message.id != message.id, Message.provider_id.is_not(None), Message.status.in_(('sent', 'simulated', 'received'))).order_by(Message.id)))
            self._event(db, workspace_id, 'mail.attempt', f'Job {job.id}, attempt {job.attempts}, message {message.id}')
            return {'job_id': job.id, 'workspace_id': workspace_id, 'message': message, 'account': account, 'contact': contact, 'references': references[-20:]}

    def _recover(self, now, workspace_id=None):
        with self.sessions() as db:
            query = select(Job.id, Job.workspace_id).where(Job.status == 'running', Job.lease_until < now)
            if workspace_id is not None:
                query = query.where(Job.workspace_id == workspace_id)
            expired = list(db.execute(query))
        for job_id, workspace_id in expired:
            with self._transaction(workspace_id) as db:
                job = db.get(Job, job_id)
                if job.status != 'running' or job.lease_until >= now:
                    continue
                message = db.get(Message, job.message_id)
                job.status = message.status = 'uncertain'
                job.error = message.error = 'Worker stopped before recording an SMTP result; reconcile before any new send'
                self._event(db, workspace_id, 'mail.uncertain', f'Job {job_id}: expired delivery lease')

    def reconcile(self, workspace_id, message_id, outcome, evidence, accepted_at=None):
        """Manually resolve an uncertain delivery, without performing any send.

        The API caller must authenticate an owner in ``workspace_id``. ``outcome``
        is ``accepted`` (provider records confirm SMTP acceptance) or ``not_sent``
        (provider records establish no acceptance). Both require 10–4000 characters
        of operator evidence. ``accepted`` also requires a verified UTC epoch time,
        between the reservation and now; it is not proof of recipient delivery.

        Returns exactly ``{status, job_id, message_id, error}``, with status ``sent``
        or ``failed``. ``not_sent`` remains failed until an explicit manual retry;
        no job is requeued here. Unknown IDs, wrong workspace, missing evidence,
        incompatible state (including repeated reconciliation), or invalid outcome
        raise ValueError without modifying records.
        """
        if outcome not in ('accepted', 'not_sent'):
            raise ValueError('Reconciliation outcome must be accepted or not_sent')
        if not isinstance(evidence, str) or not 10 <= len(evidence.strip()) <= 4000:
            raise ValueError('Provider evidence of 10 to 4000 characters is required')
        if outcome == 'accepted' and (isinstance(accepted_at, bool) or not isinstance(accepted_at, (int, float)) or not math.isfinite(accepted_at) or accepted_at > self.clock()):
            raise ValueError('A verified nonfuture UTC SMTP acceptance timestamp is required')
        with self._transaction(workspace_id) as db:
            message = db.scalar(select(Message).where(Message.id == message_id, Message.workspace_id == workspace_id))
            job = db.scalar(select(Job).where(Job.message_id == message_id, Job.workspace_id == workspace_id))
            if not message or not job or message.direction != 'outbound':
                raise ValueError('Outbound delivery does not belong to workspace')
            if message.status != 'uncertain' or job.status != 'uncertain':
                raise ValueError('Only an uncertain delivery can be reconciled')
            if outcome == 'accepted' and accepted_at < job.reserved_at:
                raise ValueError('Acceptance timestamp cannot precede this delivery attempt')
            error = '' if outcome == 'accepted' else 'Operator verified no SMTP acceptance; manual retry only'
            message.status = 'sent' if outcome == 'accepted' else 'failed'
            message.sent_at = accepted_at if outcome == 'accepted' else None
            message.error = job.error = error
            job.status = 'done' if outcome == 'accepted' else 'failed'
            contact = db.get(Contact, message.contact_id)
            if outcome == 'accepted' and contact and contact.status == 'pending':
                contact.status = 'sent'
            self._event(db, workspace_id, 'mail.reconciled', json.dumps({'message_id': message_id,
                'job_id': job.id, 'outcome': outcome, 'accepted_at': accepted_at if outcome == 'accepted' else None,
                'evidence': evidence.strip()}, ensure_ascii=False))
            return {'status': message.status, 'job_id': job.id, 'message_id': message.id, 'error': error}

    def _build_message(self, payload):
        stored, account, contact = payload['message'], payload['account'], payload['contact']
        message = EmailMessage(policy=policy.SMTP)
        message['From'] = formataddr((account.display_name or '', account.email))
        message['To'] = contact.email
        message['Subject'] = stored.subject
        message['Message-ID'] = stored.provider_id
        if stored.in_reply_to:
            message['In-Reply-To'] = stored.in_reply_to
        refs = list(dict.fromkeys(payload['references'] + _references(stored.in_reply_to)))
        if refs:
            message['References'] = ' '.join(refs[-20:])
        token = self.vault.unsubscribe_token(payload['workspace_id'], contact.email)
        link = self.public_url + '/api/unsubscribe?' + urlencode({'workspace_id': payload['workspace_id'], 'email': contact.email, 'token': token})
        message['List-Unsubscribe'] = f'<{link}>'
        message['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
        message.set_content(stored.body + '\n\nPara dejar de recibir estos correos: ' + link)
        return message

    @staticmethod
    def _smtp_open(account):
        if account.smtp_port == 465:
            return smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=30, context=ssl.create_default_context())
        server = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30)
        try:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            return server
        except Exception:
            server.close()
            raise

    def _deliver(self, account, recipient, message):
        password = self.vault.decrypt(account.secret)
        server = self._smtp_open(account)
        try:
            server.login(account.email, password)
            code, _ = server.mail(account.email)
            if code >= 400:
                raise DeliveryRejected(code, 'MAIL')
            code, _ = server.rcpt(recipient)
            if code >= 400:
                raise DeliveryRejected(code, 'RCPT')
            encoded = message.as_bytes()
            try:
                code, _ = server.data(encoded)
            except smtplib.SMTPDataError as exc:
                raise DeliveryRejected(exc.smtp_code, 'DATA') from exc
            except Exception as exc:
                raise DeliveryUncertain('SMTP connection ended during DATA; server acceptance is unknown') from exc
            if code != 250:
                raise DeliveryRejected(code, 'DATA')
        finally:
            # Closing errors cannot undo an already received DATA acknowledgement.
            try:
                server.quit()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass

    def run_once(self, workspace_id=None, *, synchronize_inbox=False):
        """Process one due message; the real worker requires a fresh full IMAP sync.

        A failed or partial sync leaves jobs unclaimed. This option belongs to
        the worker's real-delivery path; simulation never contacts mail services.
        It closes the stale local-inbox gap before the authoritative claim checks.
        """
        now = self.clock()
        self._recover(now, workspace_id)
        with self.sessions() as db:
            query = select(Job.id, Job.workspace_id).where(Job.status == 'pending', Job.due_at <= now)
            if workspace_id is not None:
                query = query.where(Job.workspace_id == workspace_id)
            due = list(db.execute(query.order_by(Job.due_at, Job.id)))
        payload = None
        synced_accounts, blocked_accounts = set(), set()
        for job_id, workspace_id in due:
            if synchronize_inbox and not self.simulation:
                with self.sessions() as db:
                    account_id = db.scalar(select(Account.id).join(Message, Message.account_id == Account.id)
                        .join(Job, Job.message_id == Message.id).join(Contact, Contact.id == Message.contact_id)
                        .join(Campaign, Campaign.id == Contact.campaign_id).join(Workspace, Workspace.id == Campaign.workspace_id)
                        .where(Job.id == job_id, Job.workspace_id == workspace_id, Account.workspace_id == workspace_id,
                            Message.workspace_id == workspace_id, Contact.workspace_id == workspace_id,
                            Campaign.workspace_id == workspace_id, Campaign.status == 'active', Account.active.is_(True),
                            Workspace.subscription_status.in_(self.SENDING_SUBSCRIPTIONS)))
                if account_id is None or account_id in blocked_accounts:
                    continue
                if account_id not in synced_accounts:
                    try:
                        synced = self.sync_account(workspace_id, account_id)
                    except Exception:
                        blocked_accounts.add(account_id)
                        continue
                    if synced.get('has_more') or synced.get('status') == 'inactive':
                        blocked_accounts.add(account_id)
                        continue
                    synced_accounts.add(account_id)
                now = self.clock()
            payload = self._claim(job_id, workspace_id, now)
            if payload:
                break
        if not payload:
            if blocked_accounts:
                return {'status': 'blocked', 'reason': 'inbox_sync', 'blocked_accounts': len(blocked_accounts)}
            return {'status': 'idle'}
        outcome, error, permanent = 'simulated' if self.simulation else 'sent', '', False
        # Recheck a cancellation/suppression received between reservation and transport.
        with self._transaction(payload['workspace_id']) as db:
            job = db.get(Job, payload['job_id'])
            contact = db.get(Contact, payload['contact'].id)
            campaign = db.get(Campaign, contact.campaign_id)
            account = db.get(Account, payload['account'].id)
            workspace = db.get(Workspace, payload['workspace_id'])
            stored = db.get(Message, job.message_id)
            suppressed = db.scalar(select(Suppression.id).where(Suppression.workspace_id == payload['workspace_id'], func.lower(Suppression.email) == contact.email.lower()))
            automatic = (payload['message'].idempotency_key or '').startswith(('first:', 'followup:'))
            replied = automatic and contact.status in ('replied', 'interested', 'not_interested')
            current = self._current_conversation(db, stored)
            if job.status != 'running' or suppressed or replied or not current or campaign.status != 'active' or not account.active:
                self._cancel(db, job, stored, 'Cancelled before transport: conversation or sending policy changed')
                return {'status': 'cancelled', 'job_id': job.id}
            if workspace.subscription_status not in self.SENDING_SUBSCRIPTIONS:
                job.status, stored.status = 'pending', 'queued'
                job.error = stored.error = 'Subscription does not allow sending'
                self._event(db, workspace.id, 'mail.subscription_blocked', f'Job {job.id}: subscription changed before transport')
                return {'status': 'blocked', 'job_id': job.id, 'message_id': stored.id, 'error': job.error}
        try:
            message = self._build_message(payload)
            if not self.simulation:
                parsed = urlparse(self.public_url)
                if parsed.scheme != 'https' or not parsed.hostname or parsed.hostname in ('localhost', '127.0.0.1', '::1'):
                    raise ValueError('Real email requires a public HTTPS unsubscribe URL')
                self._deliver(payload['account'], payload['contact'].email, message)
        except DeliveryUncertain as exc:
            outcome, error = 'uncertain', str(exc)
        except Exception as exc:
            outcome = 'failed'
            code = getattr(exc, 'code', getattr(exc, 'smtp_code', None))
            permanent = isinstance(exc, (ValueError, smtplib.SMTPAuthenticationError)) or bool(code and code >= 500)
            error = f'{type(exc).__name__}' + (f' (SMTP {code})' if code else '')
        with self._transaction(payload['workspace_id']) as db:
            job = db.get(Job, payload['job_id'])
            stored = db.get(Message, job.message_id)
            account = db.get(Account, payload['account'].id)
            contact = db.get(Contact, payload['contact'].id)
            stored.status, stored.error = outcome, error
            job.error = error
            if outcome in ('sent', 'simulated'):
                job.status = 'done'
                if outcome == 'sent':
                    stored.sent_at = self.clock()
                if contact.status == 'pending':
                    contact.status = outcome
                account.last_error = ''
            elif outcome == 'uncertain':
                job.status = 'uncertain'
                account.last_error = error
            else:
                account.last_error = error
                if not permanent and job.attempts < self.MAX_ATTEMPTS:
                    job.status, stored.status = 'pending', 'queued'
                    job.due_at = self.clock() + 60 * (2 ** (job.attempts - 1))
                else:
                    job.status = 'failed'
            self._event(db, payload['workspace_id'], f'mail.{outcome}', f'Job {job.id}, attempt {job.attempts}' + (f': {error}' if error else ''))
            return {'status': outcome, 'job_id': job.id, 'message_id': stored.id, 'error': error}

    def ingest(self, workspace_id, account_id, provider_id, from_email, subject, body, in_reply_to=''):
        """Record unique inbound mail only when its conversation is unambiguous."""
        provider_id = _identity(provider_id)
        if not provider_id:
            raise ValueError('An incoming provider message ID is required')
        sender = parseaddr(from_email)[1].lower().strip()
        with self._transaction(workspace_id) as db:
            account = db.scalar(select(Account).where(Account.id == account_id, Account.workspace_id == workspace_id))
            if not account:
                raise ValueError('Account does not belong to workspace')
            existing = db.scalar(select(Message).where(Message.account_id == account_id, Message.provider_id == provider_id))
            if existing:
                existing._egasis_duplicate = True
                return existing
            daemon = sender.split('@', 1)[0] in ('mailer-daemon', 'postmaster')
            bounced = daemon and bool(re.search(r'(?im)^(Status:\s*5\.\d+\.\d+|Diagnostic-Code:.*\b5\d\d\b)', body))
            refs = [_identity(value) for value in _references(in_reply_to)]
            contacts = []
            if refs:
                ids = set(db.scalars(select(Message.contact_id).where(Message.workspace_id == workspace_id, Message.account_id == account_id, Message.provider_id.in_(refs), Message.direction == 'outbound')))
                contacts = list(db.scalars(select(Contact).where(Contact.workspace_id == workspace_id, Contact.id.in_(ids)))) if ids else []
                contacts = [contact for contact in contacts if contact.email.lower() == sender or bounced]
            if not contacts:
                recipient = sender
                if bounced:
                    targets = re.findall(r'(?im)^(?:Final|Original)-Recipient:\s*rfc822\s*;\s*([^\s<>]+)', body)
                    if len(set(value.lower() for value in targets)) != 1:
                        return None
                    recipient = targets[0].lower()
                contacts = list(db.scalars(select(Contact).where(Contact.workspace_id == workspace_id, Contact.account_id == account_id, func.lower(Contact.email) == recipient)))
            if len(contacts) != 1:
                self._event(db, workspace_id, 'mail.ignored', 'Incoming mail has no unique conversation for the receiving account')
                return None
            contact = contacts[0]
            if bounced and not db.scalar(select(Message.id).where(Message.workspace_id == workspace_id, Message.account_id == account_id, Message.contact_id == contact.id, Message.direction == 'outbound', Message.status.in_(('sent', 'simulated', 'uncertain', 'sending')))):
                return None
            unsubscribe = not bounced and _unsubscribe_requested(body)
            classification = 'bounce' if bounced else 'unsubscribe' if unsubscribe else 'unclassified'
            incoming = Message(workspace_id=workspace_id, contact_id=contact.id, account_id=account_id, direction='inbound', subject=(subject or '')[:10_000], body=(body or '')[:200_000], status='received', classification=classification, provider_id=provider_id, in_reply_to=_identity(refs[-1] if refs else ''))
            db.add(incoming)
            previously_suppressed = db.scalar(select(Suppression.id).where(Suppression.workspace_id == workspace_id, func.lower(Suppression.email) == contact.email.lower()))
            contact.status = 'bounced' if bounced else 'do_not_contact' if unsubscribe or previously_suppressed else 'replied'
            affected = [contact.id]
            if unsubscribe or bounced:
                if not db.scalar(select(Suppression.id).where(Suppression.workspace_id == workspace_id, func.lower(Suppression.email) == contact.email.lower())):
                    db.add(Suppression(workspace_id=workspace_id, email=contact.email.lower(), reason=classification))
                related = list(db.scalars(select(Contact).where(Contact.workspace_id == workspace_id, func.lower(Contact.email) == contact.email.lower())))
                affected = [other.id for other in related]
                for other in related:
                    other.status = 'bounced' if bounced else 'do_not_contact'
            queued = db.execute(select(Job, Message).join(Message, Message.id == Job.message_id).where(Job.workspace_id == workspace_id, Message.contact_id.in_(affected), Job.status == 'pending')).all()
            for job, outbound in queued:
                if unsubscribe or bounced or (outbound.idempotency_key or '').startswith(('first:', 'followup:')):
                    self._cancel(db, job, outbound, 'Recipient replied' if not (unsubscribe or bounced) else 'Recipient suppressed')
            self._event(db, workspace_id, 'mail.received', f'Contact {contact.id}, classification {classification}')
            db.flush()
            incoming._egasis_duplicate = False
            return incoming

    def sync_account(self, workspace_id, account_id):
        with self.sessions() as db:
            account = db.scalar(select(Account).where(Account.id == account_id, Account.workspace_id == workspace_id))
            if not account:
                raise ValueError('Account does not belong to workspace')
            if not account.active:
                return {'imported': 0, 'duplicates': 0, 'ignored': 0, 'status': 'inactive'}
        result = {'imported': 0, 'duplicates': 0, 'ignored': 0}
        mailbox = None
        try:
            password = self.vault.decrypt(account.secret)
            mailbox = self.imap_factory(account.imap_host, account.imap_port, timeout=30, ssl_context=ssl.create_default_context())
            mailbox.login(account.email, password)
            status, _ = mailbox.select('INBOX', readonly=True)
            if status != 'OK':
                raise ValueError('Cannot select inbox')
            _, validity_data = mailbox.response('UIDVALIDITY')
            validity = (validity_data[0].decode() if validity_data and validity_data[0] else '')
            if not validity:
                raise ValueError('IMAP server omitted UIDVALIDITY')
            cursor = account.imap_last_uid if account.imap_uidvalidity == validity else 0
            status, rows = mailbox.uid('search', None, 'UID', f'{cursor + 1}:*')
            if status != 'OK':
                raise ValueError('IMAP UID search failed')
            available_uids = sorted({int(uid) for uid in (rows[0] or b'').split() if int(uid) > cursor})
            uids = available_uids[:200]
            has_more = len(available_uids) > len(uids)
            for uid in uids:
                status, chunks = mailbox.uid('fetch', str(uid), '(BODY.PEEK[])')
                raw = next((chunk[1] for chunk in (chunks or []) if isinstance(chunk, tuple) and isinstance(chunk[1], bytes)), None)
                if status != 'OK' or raw is None:
                    raise ValueError('IMAP message fetch failed')
                parsed = BytesParser(policy=policy.default).parsebytes(raw)
                message_id = str(parsed.get('Message-ID', '')).strip() or f'imap:{validity}:{uid}'
                refs = str(parsed.get('In-Reply-To', '') or parsed.get('References', ''))
                incoming = self.ingest(workspace_id, account_id, message_id, str(parsed.get('From', '')), str(parsed.get('Subject', '')), _plain_body(parsed), refs)
                result['ignored' if incoming is None else 'duplicates' if incoming._egasis_duplicate else 'imported'] += 1
                with self._transaction(workspace_id) as db:
                    saved = db.get(Account, account_id)
                    if saved.imap_uidvalidity == validity:
                        saved.imap_last_uid = max(saved.imap_last_uid, uid)
                    else:
                        saved.imap_uidvalidity, saved.imap_last_uid = validity, uid
            # Even an empty successful poll clears a previous IMAP error. SMTP
            # errors stay visible until a later SMTP result resolves them.
            with self._transaction(workspace_id) as db:
                saved = db.get(Account, account_id)
                if saved.imap_uidvalidity != validity and not uids:
                    saved.imap_uidvalidity, saved.imap_last_uid = validity, 0
                if has_more:
                    saved.last_error = 'IMAP: inbox backlog; delivery waits for complete synchronization'
                elif (saved.last_error or '').startswith('IMAP:'):
                    saved.last_error = ''
            if has_more:
                result['has_more'] = True
            return result
        except Exception as exc:
            with self._transaction(workspace_id) as db:
                db.get(Account, account_id).last_error = 'IMAP: ' + type(exc).__name__
            raise
        finally:
            if mailbox:
                try:
                    mailbox.logout()
                except Exception:
                    pass
