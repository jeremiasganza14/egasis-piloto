"""Durable workspace-scoped records. All timestamps are UTC epoch seconds."""
import time
from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Workspace(Base):
    __tablename__ = 'workspaces'
    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    offer = Column(Text, default='')
    audience = Column(Text, default='')
    signature = Column(Text, default='')
    timezone = Column(String(80), default='America/Argentina/Buenos_Aires')
    daily_budget = Column(Float, default=5.0)
    plan = Column(String(30), default='pilot')
    subscription_status = Column(String(30), default='pilot')
    stripe_customer = Column(String(100), nullable=True, unique=True)
    stripe_subscription = Column(String(100), nullable=True)
    created_at = Column(Float, default=time.time)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    email = Column(String(254), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(String(20), default='owner')

class Session(Base):
    __tablename__ = 'sessions'
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(ForeignKey('users.id'), nullable=False)
    expires_at = Column(Float, nullable=False)

class Campaign(Base):
    __tablename__ = 'campaigns'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    name = Column(String(160), nullable=False)
    offer = Column(Text, default='')
    audience = Column(Text, default='')
    subject = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    status = Column(String(20), default='draft')
    daily_limit = Column(Integer, default=20)
    start_hour = Column(Integer, default=9)
    end_hour = Column(Integer, default=18)
    weekdays = Column(String(30), default='0,1,2,3,4')
    reply_mode = Column(String(20), default='review')
    auto_reply_body = Column(Text, default='')
    created_at = Column(Float, default=time.time)

class Account(Base):
    __tablename__ = 'accounts'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    email = Column(String(254), nullable=False)
    display_name = Column(String(160), default='')
    secret = Column(Text, nullable=False)
    smtp_host = Column(String(254), default='smtp.gmail.com')
    smtp_port = Column(Integer, default=587)
    imap_host = Column(String(254), default='imap.gmail.com')
    imap_port = Column(Integer, default=993)
    daily_limit = Column(Integer, default=30)
    cooldown_seconds = Column(Integer, default=120)
    last_sent_at = Column(Float, default=0)
    active = Column(Boolean, default=True)
    last_error = Column(Text, default='')
    imap_uidvalidity = Column(String(80), default='')
    imap_last_uid = Column(Integer, default=0)
    __table_args__ = (UniqueConstraint('workspace_id', 'email'),)

class Contact(Base):
    __tablename__ = 'contacts'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    campaign_id = Column(ForeignKey('campaigns.id'), nullable=False, index=True)
    account_id = Column(ForeignKey('accounts.id'), nullable=True)
    email = Column(String(254), nullable=False)
    name = Column(String(160), default='')
    company = Column(String(200), default='')
    website = Column(Text, default='')
    source = Column(String(100), default='manual')
    evidence = Column(Text, default='')
    score = Column(Integer, nullable=True)
    fit_reason = Column(Text, default='')
    status = Column(String(30), default='pending')
    created_at = Column(Float, default=time.time)
    __table_args__ = (UniqueConstraint('workspace_id', 'campaign_id', 'email'),)

class Message(Base):
    __tablename__ = 'messages'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    contact_id = Column(ForeignKey('contacts.id'), nullable=False, index=True)
    account_id = Column(ForeignKey('accounts.id'), nullable=True)
    direction = Column(String(20), nullable=False)
    subject = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    status = Column(String(30), default='draft')
    classification = Column(String(30), default='unclassified')
    provider_id = Column(String(255), nullable=True)
    in_reply_to = Column(String(255), default='')
    idempotency_key = Column(String(255), nullable=True)
    error = Column(Text, default='')
    created_at = Column(Float, default=time.time)
    sent_at = Column(Float, nullable=True)
    __table_args__ = (UniqueConstraint('workspace_id', 'idempotency_key'), UniqueConstraint('account_id', 'provider_id'))

class Job(Base):
    __tablename__ = 'jobs'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    message_id = Column(ForeignKey('messages.id'), unique=True, nullable=False)
    status = Column(String(30), default='pending')
    attempts = Column(Integer, default=0)
    due_at = Column(Float, default=time.time)
    lease_until = Column(Float, default=0)
    reserved_at = Column(Float, default=0)
    error = Column(Text, default='')

class Suppression(Base):
    __tablename__ = 'suppressions'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    email = Column(String(254), nullable=False)
    reason = Column(String(100), nullable=False)
    created_at = Column(Float, default=time.time)
    __table_args__ = (UniqueConstraint('workspace_id', 'email'),)

class Meeting(Base):
    __tablename__ = 'meetings'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    contact_id = Column(ForeignKey('contacts.id'), nullable=False)
    starts_at = Column(Float, nullable=False)
    duration_minutes = Column(Integer, default=30)
    status = Column(String(30), default='proposed')
    location = Column(Text, default='')
    notes = Column(Text, default='')
    external_id = Column(String(255), nullable=True)

class Usage(Base):
    __tablename__ = 'usage'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    contact_id = Column(ForeignKey('contacts.id'), nullable=True)
    operation = Column(String(80), nullable=False)
    model = Column(String(80), nullable=False)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cost = Column(Float, default=0)
    created_at = Column(Float, default=time.time)

class Event(Base):
    __tablename__ = 'events'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    kind = Column(String(80), nullable=False)
    detail = Column(Text, default='')
    created_at = Column(Float, default=time.time)

class WebhookEvent(Base):
    __tablename__ = 'webhook_events'
    id = Column(String(255), primary_key=True)
    created_at = Column(Float, default=time.time)

class Connection(Base):
    __tablename__ = 'connections'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    provider = Column(String(50), nullable=False)
    secret = Column(Text, nullable=False)
    __table_args__ = (UniqueConstraint('workspace_id', 'provider'),)

class KnowledgeNote(Base):
    __tablename__ = 'knowledge_notes'
    id = Column(Integer, primary_key=True)
    workspace_id = Column(ForeignKey('workspaces.id'), nullable=False, index=True)
    text = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default='draft')
    source_message_id = Column(ForeignKey('messages.id'), nullable=True, index=True)
    created_at = Column(Float, nullable=False, default=time.time)
    updated_at = Column(Float, nullable=False, default=time.time)
    approved_at = Column(Float, nullable=True)
    archived_at = Column(Float, nullable=True)
