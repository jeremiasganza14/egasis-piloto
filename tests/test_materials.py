import json
import time
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from egasis.materials import build_brief, export_markdown, export_pptx
from egasis.models import Campaign, Contact, Meeting, Message, Suppression, Workspace
from egasis.store import make_store


@pytest.fixture
def setup(tmp_path):
    engine, factory = make_store('sqlite:///' + str(tmp_path / 'materials.sqlite'))
    rows = []
    with factory() as db:
        for name, offer, audience, company in [
            ('Estudio Contable Sur', 'Liquidación de impuestos y asesoría contable', 'Comercios minoristas', 'Almacén Las Flores'),
            ('Equipos del Centro', 'Equipamiento industrial y mantenimiento de maquinaria', 'Plantas de fabricación', 'Fábrica Horizonte'),
        ]:
            workspace = Workspace(name=name, offer=offer, audience=audience, signature=name, timezone='UTC')
            db.add(workspace); db.flush()
            campaign = Campaign(workspace_id=workspace.id, name='Presentación', subject='Conversación', body='Oferta configurada')
            db.add(campaign); db.flush()
            contact = Contact(workspace_id=workspace.id, campaign_id=campaign.id, name='Ana', company=company,
                              email=f'contact-{workspace.id}@example.com', website='https://example.com',
                              evidence=json.dumps({'text': f'{company} describe su actividad en este sitio.',
                                                   'url': 'https://example.com', 'retrieved_at': 1_780_000_000}))
            db.add(contact); db.flush()
            db.add_all([
                Message(workspace_id=workspace.id, contact_id=contact.id, direction='outbound', status='sent',
                        subject='Nuestra propuesta', body='Podemos conversar sobre ' + offer, sent_at=time.time()),
                Message(workspace_id=workspace.id, contact_id=contact.id, direction='inbound', status='received',
                        subject='Consulta', body='¿Qué alcance tiene el servicio? Podemos conversar la semana próxima.'),
                Message(workspace_id=workspace.id, contact_id=contact.id, direction='outbound', status='draft',
                        subject='Sin aprobar', body='UNAPPROVED_SECRET_DRAFT: el precio es USD 999 y el trato está cerrado.'),
            ])
            rows.append((workspace.id, contact.id, campaign.id, name, offer, company))
        db.commit()
    yield factory, rows
    engine.dispose()


def deck_text(blob, include_notes=False):
    with ZipFile(blob) as archive:
        text = []
        for name in archive.namelist():
            if (name.startswith('ppt/slides/slide') or (include_notes and name.startswith('ppt/notesSlides/notesSlide'))) and name.endswith('.xml'):
                text.extend(item.text or '' for item in ET.fromstring(archive.read(name)).iter('{http://schemas.openxmlformats.org/drawingml/2006/main}t'))
    return '\n'.join(text)


@pytest.mark.parametrize('index', [0, 1])
def test_materials_follow_each_business_and_never_a_fixed_niche(setup, index):
    factory, rows = setup
    wid, cid, _, name, offer, company = rows[index]
    brief = build_brief(factory, wid, cid)
    md = export_markdown(brief).getvalue().decode()
    pptx = export_pptx(brief)
    text = deck_text(pptx, include_notes=True)
    assert offer in text and name in text and company in text
    assert offer in md and name in md and company in md
    assert rows[1-index][3] not in text and rows[1-index][4] not in md
    assert 'UNAPPROVED_SECRET_DRAFT' not in text and 'UNAPPROVED_SECRET_DRAFT' not in md
    assert 'USD 999' not in text and 'USD 999' not in md
    assert 'automatización con IA' not in text
    assert brief['campaign']['offer_source'] == 'workspace'
    assert len(brief['messages']) == 2


def test_campaign_offer_overrides_workspace_without_inventing_other_claims(setup):
    factory, rows = setup
    wid, cid, campaign, _, _, _ = rows[0]
    with factory() as db:
        db.get(Campaign, campaign).offer = 'Servicio específico de auditoría documental'
        db.get(Campaign, campaign).audience = 'Distribuidoras'
        db.commit()
    brief = build_brief(factory, wid, cid)
    assert brief['campaign']['offer'] == 'Servicio específico de auditoría documental'
    assert brief['campaign']['audience'] == 'Distribuidoras'
    assert brief['campaign']['offer_source'] == 'campaign'


def test_cross_tenant_contact_and_corrupt_campaign_are_rejected(setup):
    factory, rows = setup
    wid, cid, _, _, _, _ = rows[0]
    with pytest.raises(ValueError):
        build_brief(factory, wid, rows[1][1])
    with factory() as db:
        db.get(Contact, cid).campaign_id = rows[1][2]; db.commit()
    with pytest.raises(ValueError):
        build_brief(factory, wid, cid)


def test_wrong_tenant_related_messages_and_meetings_are_never_exported(setup):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    with factory() as db:
        db.add(Message(workspace_id=rows[1][0], contact_id=cid, direction='inbound', status='received',
                       subject='OTHER_TENANT', body='OTHER_TENANT_PRIVATE_MESSAGE'))
        db.add(Meeting(workspace_id=rows[1][0], contact_id=cid, starts_at=time.time()+86400,
                       status='confirmed', notes='OTHER_TENANT_PRIVATE_MEETING'))
        db.commit()
    brief = build_brief(factory, wid, cid)
    assert 'OTHER_TENANT' not in json.dumps(brief)
    assert not brief['meetings']


def test_email_claim_and_booking_link_cannot_create_confirmed_meeting(setup):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    with factory() as db:
        db.add(Message(workspace_id=wid, contact_id=cid, direction='inbound', status='received',
                       subject='Horario', body='La reunión está confirmada, mirá https://calendar.example.com/book'))
        db.commit()
    brief = build_brief(factory, wid, cid)
    assert brief['next_step']['status'] == 'unconfirmed'
    assert not brief['meetings']
    assert 'Egasis no registra una reunión futura confirmada.' in deck_text(export_pptx(brief))


@pytest.mark.parametrize('status, expected', [('proposed', 'proposed'), ('confirmed', 'confirmed'), ('cancelled', 'unconfirmed')])
def test_meeting_status_comes_only_from_scoped_record(setup, status, expected):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    with factory() as db:
        db.add(Meeting(workspace_id=wid, contact_id=cid, starts_at=time.time()+86400,
                       duration_minutes=45, status=status, location='Sala Norte'))
        db.commit()
    brief = build_brief(factory, wid, cid)
    assert brief['next_step']['status'] == expected
    if expected != 'unconfirmed':
        assert 'Sala Norte' in brief['next_step']['detail']
        assert '45 minutos' in brief['next_step']['detail']


def test_past_confirmed_meeting_does_not_imply_future_booking(setup):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    with factory() as db:
        db.add(Meeting(workspace_id=wid, contact_id=cid, starts_at=time.time()-86400, status='confirmed'))
        db.commit()
    assert build_brief(factory, wid, cid)['next_step']['status'] == 'unconfirmed'


def test_suppression_prevents_outreach_recommendation(setup):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    with factory() as db:
        db.add(Suppression(workspace_id=wid, email=db.get(Contact, cid).email, reason='unsubscribe')); db.commit()
    brief = build_brief(factory, wid, cid)
    assert brief['next_step']['status'] == 'suppressed'
    assert 'Contacto excluido de nuevos mensajes' in deck_text(export_pptx(brief))


def test_unattributed_evidence_and_missing_offer_are_explicit(setup):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    with factory() as db:
        db.get(Contact, cid).evidence = 'MALFORMED_LEGACY_CLAIM'
        db.get(Workspace, wid).offer = ''
        db.commit()
    brief = build_brief(factory, wid, cid)
    text = deck_text(export_pptx(brief))
    assert brief['evidence'] == []
    assert 'MALFORMED_LEGACY_CLAIM' not in text
    assert 'Oferta pendiente de configurar.' in text
    assert 'Sin investigación atribuida' in text


def test_markdown_source_content_cannot_embed_html_or_remote_images(setup):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    with factory() as db:
        db.get(Contact, cid).name = '<script>alert(1)</script>![pixel](https://tracker.invalid/x)'
        db.commit()
    content = export_markdown(build_brief(factory, wid, cid)).getvalue().decode()
    assert '<script>' not in content and '![pixel]' not in content
    assert '&lt;script&gt;' in content


def test_pptx_has_five_editable_slides_notes_no_external_assets_and_no_template_tokens(setup):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    result = export_pptx(build_brief(factory, wid, cid))
    assert result.tell() == 0
    with ZipFile(result) as archive:
        slides = [name for name in archive.namelist() if name.startswith('ppt/slides/slide') and name.endswith('.xml')]
        notes = [name for name in archive.namelist() if name.startswith('ppt/notesSlides/notesSlide') and name.endswith('.xml')]
        assert len(slides) == 5 and len(notes) == 5
        for name in slides + notes:
            root = ET.fromstring(archive.read(name))
            assert list(root.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}t'))
            assert b'{{' not in archive.read(name)
        for name in archive.namelist():
            if name.endswith('.rels'):
                assert b'TargetMode="External"' not in archive.read(name)


def test_long_text_is_excerpted_on_slides_and_preserved_in_notes(setup):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    long_offer = 'Servicio de mantenimiento preventivo para maquinaria especializada. ' * 40 + 'FINAL_DEL_TEXTO'
    with factory() as db:
        db.get(Workspace, wid).offer = long_offer; db.commit()
    brief = build_brief(factory, wid, cid)
    assert 'FINAL_DEL_TEXTO' not in deck_text(export_pptx(brief))
    assert 'FINAL_DEL_TEXTO' in deck_text(export_pptx(brief), include_notes=True)


def test_simulated_outbound_is_never_described_as_real_send(setup):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    with factory() as db:
        db.add(Message(workspace_id=wid, contact_id=cid, direction='outbound', status='simulated',
                       subject='Ensayo', body='Salida de prueba sin envío real.')); db.commit()
    brief = build_brief(factory, wid, cid)
    assert 'ÚLTIMA SALIDA SIMULADA' in deck_text(export_pptx(brief))
    assert 'Salida simulada' in export_markdown(brief).getvalue().decode()


def test_invalid_xml_characters_in_source_data_cannot_break_pptx(setup):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    brief = build_brief(factory, wid, cid)
    brief['contact']['name'] = 'Ana\ud800\uffff\u0000 Fernández'
    result = export_pptx(brief)
    assert 'Ana Fernández' in deck_text(result)


def test_wide_unbroken_fields_fit_slide_line_budgets_and_keep_full_notes(setup):
    factory, rows = setup
    wid, cid, *_ = rows[0]
    brief = build_brief(factory, wid, cid)
    brief['contact']['company'] = 'W' * 200
    brief['campaign']['offer'] = 'W' * 5000
    blob = export_pptx(brief)
    with ZipFile(blob) as archive:
        for slide, allowed_lines, max_per_line in [(1, 2, 16), (2, 5, 30)]:
            parts = [node.text or '' for node in ET.fromstring(archive.read(f'ppt/slides/slide{slide}.xml')).iter(
                '{http://schemas.openxmlformats.org/drawingml/2006/main}t')]
            excerpt = next(value for value in parts if value.startswith('WW'))
            assert len(excerpt.splitlines()) <= allowed_lines
            assert all(len(line) <= max_per_line + 1 for line in excerpt.splitlines())
            assert excerpt.endswith('…')
    assert 'W' * 200 in deck_text(export_pptx(brief), include_notes=True)
