"""Owner-reviewed workspace reference notes; never automatic instructions.

The API caller must authenticate the workspace owner. All record and source
lookups are separately scoped here. Approval is required before use by AI.
"""
import json
import time
from contextlib import contextmanager

from sqlalchemy import func, select, text as sql_text

from .models import Contact, Event, KnowledgeNote, Message, Workspace


MAX_NOTE_CHARS = 2000
MAX_APPROVED_NOTES = 100
MAX_CONTEXT_CHARS = 12000


class KnowledgeError(ValueError):
    def __init__(self, message, status_code=422):
        super().__init__(message)
        self.status_code = status_code


def note_record(note):
    return {column.name: getattr(note, column.name) for column in note.__table__.columns}


def approved_context(db, workspace_id):
    """Return whole, recent approved notes within a bounded prompt budget.

    Metadata explains that these are reviewed reference data. Source emails are
    not copied into prompts. Complete notes avoid changing meaning by truncation.
    """
    rows = db.scalars(select(KnowledgeNote).where(KnowledgeNote.workspace_id == workspace_id,
        KnowledgeNote.status == 'approved').order_by(KnowledgeNote.approved_at.desc(), KnowledgeNote.id.desc())
        .limit(MAX_APPROVED_NOTES)).all()
    result, used = [], 0
    for row in rows:
        if not row.text or len(row.text) > MAX_NOTE_CHARS or used + len(row.text) > MAX_CONTEXT_CHARS:
            continue
        if row.source_message_id is not None and not db.scalar(select(Message.id).join(Contact, Contact.id == Message.contact_id).where(
                Message.id == row.source_message_id, Message.workspace_id == workspace_id,
                Message.direction == 'inbound', Contact.workspace_id == workspace_id)):
            continue
        result.append({'id': row.id, 'text': row.text, 'source_message_id': row.source_message_id,
                       'approved_at': row.approved_at, 'kind': 'owner_reviewed_reference'})
        used += len(row.text)
    return result


class KnowledgeService:
    def __init__(self, factory):
        self.factory = factory

    @contextmanager
    def _transaction(self, workspace_id):
        with self.factory() as db:
            if db.bind.dialect.name == 'sqlite':
                db.execute(sql_text('BEGIN IMMEDIATE'))
            try:
                if not db.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update()):
                    raise KnowledgeError('Espacio de trabajo inexistente.', 404)
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _source(self, db, workspace_id, message_id):
        if message_id is None:
            return
        if type(message_id) is not int or message_id <= 0 or not db.scalar(select(Message.id).join(Contact, Contact.id == Message.contact_id).where(
                Message.id == message_id, Message.workspace_id == workspace_id,
                Message.direction == 'inbound', Contact.workspace_id == workspace_id)):
            raise KnowledgeError('La fuente debe ser una respuesta recibida dentro de este espacio.', 404)

    def create(self, workspace_id, text, source_message_id=None):
        if not isinstance(text, str) or not 10 <= len(text.strip()) <= MAX_NOTE_CHARS:
            raise KnowledgeError('Escribí una nota de entre 10 y 2000 caracteres.')
        with self._transaction(workspace_id) as db:
            self._source(db, workspace_id, source_message_id)
            now = time.time()
            note = KnowledgeNote(workspace_id=workspace_id, text=text.strip(), status='draft',
                                 source_message_id=source_message_id, created_at=now, updated_at=now)
            db.add(note); db.flush()
            self._audit(db, note, 'created')
            return note_record(note)

    def list(self, workspace_id, status=None):
        if status is not None and status not in {'draft', 'approved', 'archived'}:
            raise KnowledgeError('Estado de nota desconocido.')
        with self.factory() as db:
            if not db.get(Workspace, workspace_id):
                raise KnowledgeError('Espacio de trabajo inexistente.', 404)
            query = select(KnowledgeNote).where(KnowledgeNote.workspace_id == workspace_id)
            if status is not None:
                query = query.where(KnowledgeNote.status == status)
            return [note_record(note) for note in db.scalars(query.order_by(KnowledgeNote.updated_at.desc(), KnowledgeNote.id.desc()))]

    def change_state(self, workspace_id, note_id, status):
        if status not in {'approved', 'archived'}:
            raise KnowledgeError('La acción debe aprobar o archivar la nota.')
        with self._transaction(workspace_id) as db:
            note = db.scalar(select(KnowledgeNote).where(KnowledgeNote.id == note_id,
                                                        KnowledgeNote.workspace_id == workspace_id).with_for_update())
            if not note:
                raise KnowledgeError('Nota inexistente.', 404)
            if note.status == status:
                return note_record(note)
            if note.status == 'archived':
                raise KnowledgeError('Una nota archivada no puede volver a aprobarse; creá una nueva versión.', 409)
            if status == 'approved':
                self._source(db, workspace_id, note.source_message_id)
                count = db.scalar(select(func.count()).select_from(KnowledgeNote).where(
                    KnowledgeNote.workspace_id == workspace_id, KnowledgeNote.status == 'approved'))
                if count >= MAX_APPROVED_NOTES:
                    raise KnowledgeError('Este espacio ya tiene 100 notas aprobadas. Archivá una antes de aprobar otra.', 409)
                note.approved_at = time.time()
            else:
                note.archived_at = time.time()
            note.status, note.updated_at = status, time.time()
            self._audit(db, note, status)
            return note_record(note)

    def approve(self, workspace_id, note_id):
        return self.change_state(workspace_id, note_id, 'approved')

    def archive(self, workspace_id, note_id):
        return self.change_state(workspace_id, note_id, 'archived')

    def context(self, workspace_id):
        with self.factory() as db:
            if not db.get(Workspace, workspace_id):
                raise KnowledgeError('Espacio de trabajo inexistente.', 404)
            return approved_context(db, workspace_id)

    @staticmethod
    def _audit(db, note, action):
        db.add(Event(workspace_id=note.workspace_id, kind='knowledge.' + action, detail=json.dumps({
            'note_id': note.id, 'source_message_id': note.source_message_id, 'status': note.status,
        }, sort_keys=True)))
