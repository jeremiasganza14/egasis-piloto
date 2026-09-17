"""HTTP product regressions; isolated workspaces, no SMTP/IMAP/AI/Stripe calls."""
import time
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from egasis.app import create_app
from egasis.models import Campaign, Contact, Job, Message, Suppression, User
from egasis.settings import Settings

@pytest.fixture
def app(tmp_path):
    return create_app(Settings(f'sqlite:///{tmp_path / "api.db"}', 'isolated-test-key'))

@pytest.fixture
def client(app):
    with TestClient(app) as c:
        result=c.post('/api/register',json={'name':'Comercial Norte','email':'owner@north.example','password':'A-long-test-password'})
        assert result.status_code==200
        yield c

def campaign(client,**overrides):
    body={'name':'Servicios contables','offer':'Asesoría contable','subject':'Una idea para {{company}}','body':'Hola {{name}}, ofrecemos {{offer}}. {{signature}}','start_hour':0,'end_hour':24,'weekdays':'0,1,2,3,4,5,6'}
    body.update(overrides)
    response=client.post('/api/campaigns',json=body)
    assert response.status_code==200,response.text
    return response.json()

def account(client):
    response=client.post('/api/accounts',json={'email':'team@north.example','password':'test-secret-account'})
    assert response.status_code==200,response.text
    return response.json()

def contact(client,cid,email='a@company.example'):
    result=client.post(f'/api/contacts/import/{cid}',json={'contacts':[{'email':email,'company':'Company','name':'Ana'}]})
    assert result.status_code==200,result.text
    return client.get('/api/contacts').json()[0]

def inbound(app,client,c):
    with app.state.factory() as db:
        row=Message(workspace_id=client.get('/api/me').json()['workspace']['id'],contact_id=c['id'],account_id=account(client)['id'],direction='inbound',subject='Re: Consulta',body='Me interesa saber más.',provider_id='<incoming@company.example>',status='received')
        db.add(row);db.commit();return row.id

def test_authentication_and_cookie(client):
    result=client.get('/api/me');assert result.status_code==200
    assert 'password_hash' not in result.text
    assert client.cookies.get('egasis_session')
    client.post('/api/logout')
    assert client.get('/api/me').status_code==401
    assert client.post('/api/login',json={'email':'owner@north.example','password':'wrong'}).status_code==401
    result=client.post('/api/login',json={'email':'owner@north.example','password':'A-long-test-password'})
    assert result.status_code==200
    assert 'httponly' in result.headers['set-cookie'].lower()
    assert 'samesite=strict' in result.headers['set-cookie'].lower()

def test_public_assets_revalidate_and_private_responses_are_not_cached(client):
    for route in ['/', '/book', '/static/app.js', '/static/style.css']:
        response=client.get(route)
        assert response.status_code==200
        assert response.headers['cache-control']=='no-cache'
    assert client.get('/api/me').headers['cache-control']=='no-store'

def test_cross_origin_mutation_rejected(client):
    assert client.post('/api/demo',headers={'Origin':'https://attacker.invalid'}).status_code==403

def test_tenant_routes_never_return_other_customer_resources(app,client):
    c=campaign(client);person=contact(client,c['id']);a=account(client)
    with TestClient(app) as other:
        other.post('/api/register',json={'name':'Other','email':'owner@other.example','password':'A-long-test-password'})
        for route in ['/api/campaigns','/api/contacts','/api/accounts','/api/meetings','/api/conversations']:
            assert other.get(route).json()==[]
        for route in [f'/api/campaigns/{c["id"]}/start',f'/api/contacts/{person["id"]}/suppress',f'/api/accounts/{a["id"]}/toggle']:
            assert other.post(route).status_code==404
        assert person['email'] not in other.get('/api/export/contacts').text
        assert other.get('/api/metrics').json()['contacts']==0

def test_first_email_automatic_only_after_campaign_activation(app,client):
    account(client);c=campaign(client);contact(client,c['id'])
    assert client.post('/api/engine/tick').json()['result']['status']=='idle'
    assert client.post(f'/api/campaigns/{c["id"]}/start').json()['queued']==1
    with patch('smtplib.SMTP',side_effect=AssertionError('Live transport forbidden')):
        result=client.post('/api/engine/tick')
    assert result.status_code==200,result.text
    assert result.json()['result']['status']=='simulated'
    metrics=client.get('/api/metrics').json()
    assert metrics['sent']==0 and metrics['simulated']==1 and metrics['replies']==0
    client.post(f'/api/campaigns/{c["id"]}/start')
    assert client.post('/api/engine/tick').json()['result']['status']=='idle'

def test_simulation_tick_scoped_to_current_owner(app,client):
    account(client);c=campaign(client);contact(client,c['id'])
    client.post(f'/api/campaigns/{c["id"]}/start')
    with TestClient(app) as other:
        other.post('/api/register',json={'name':'Other','email':'owner@other.example','password':'A-long-test-password'})
        result=other.post('/api/engine/tick')
        assert result.json()['result']['status']=='idle'
    assert client.get('/api/metrics').json()['queued']==1

def test_pause_prevents_sending_and_import_requires_pause(client):
    account(client);c=campaign(client);contact(client,c['id'])
    client.post(f'/api/campaigns/{c["id"]}/start')
    assert client.post(f'/api/contacts/import/{c["id"]}',json={'contacts':[]}).status_code==409
    client.post(f'/api/campaigns/{c["id"]}/pause')
    assert client.post('/api/engine/tick').json()['result']['status']=='idle'

def test_secret_encrypted_and_never_returned(app,client):
    a=account(client)
    assert 'secret' not in a and 'test-secret-account' not in client.get('/api/accounts').text
    from egasis.models import Account
    with app.state.factory() as db:
        row=db.get(Account,a['id'])
        assert row.secret!='test-secret-account'
        assert app.state.vault.decrypt(row.secret)=='test-secret-account'

def test_unsupported_private_account_server_rejected(client):
    assert client.post('/api/accounts',json={'email':'a@b.example','password':'secret','smtp_host':'127.0.0.1'}).status_code==422

def test_duplicate_contact_and_suppression_persist_across_campaigns(app,client):
    c=campaign(client);person=contact(client,c['id'])
    result=client.post(f'/api/contacts/import/{c["id"]}',json={'contacts':[{'email':person['email'].upper()}]})
    assert result.json()=={'imported':0,'duplicates':1}
    client.post(f'/api/contacts/{person["id"]}/suppress')
    second=campaign(client,name='Otro enfoque');imported=contact(client,second['id'])
    assert imported['status']=='do_not_contact'

def test_unsubscribe_get_does_not_mutate_post_suppresses(app,client):
    c=campaign(client);person=contact(client,c['id']);wid=client.get('/api/me').json()['workspace']['id']
    params={'workspace_id':wid,'email':person['email'],'token':app.state.vault.unsubscribe_token(wid,person['email'])}
    assert client.get('/api/unsubscribe',params=params).status_code==200
    assert client.get('/api/contacts').json()[0]['status']=='pending'
    assert client.post('/api/unsubscribe',params=params).status_code==200
    assert client.get('/api/contacts').json()[0]['status']=='do_not_contact'
    params['token']='forged';assert client.post('/api/unsubscribe',params=params).status_code==403

def test_approved_reply_is_queued_and_idempotent(app,client):
    c=campaign(client);person=contact(client,c['id']);mid=inbound(app,client,person)
    payload={'subject':'Re: Consulta','body':'Gracias, Ana. ¿Qué información necesitás?'}
    first=client.post(f'/api/messages/{mid}/reply',json=payload)
    second=client.post(f'/api/messages/{mid}/reply',json=payload)
    assert first.status_code==200,first.text
    assert first.json()['id']==second.json()['id']
    assert first.json()['status']=='queued'
    assert client.get('/api/metrics').json()['sent']==0
    with app.state.factory() as db:
        assert len(db.scalars(select(Job)).all())==1

def test_suppressed_contact_cannot_receive_reply(app,client):
    c=campaign(client);person=contact(client,c['id']);mid=inbound(app,client,person)
    client.post(f'/api/contacts/{person["id"]}/suppress')
    assert client.post(f'/api/messages/{mid}/reply',json={'subject':'Hello','body':'Test'}).status_code==409

def test_meeting_requires_explicit_confirmation_and_no_overlaps(client):
    c=campaign(client);person=contact(client,c['id']);payload={'contact_id':person['id'],'starts_at':time.time()+7200}
    first=client.post('/api/meetings',json=payload).json()
    assert first['status']=='proposed' and client.get('/api/metrics').json()['meetings']==0
    assert client.post(f'/api/meetings/{first["id"]}/confirm').status_code==200
    second=client.post('/api/meetings',json=payload).json()
    assert client.post(f'/api/meetings/{second["id"]}/confirm').status_code==409
    assert 'STATUS:CONFIRMED' in client.get(f'/api/calendar/{first["id"]}.ics').text

def test_template_header_injection_and_automatic_requirements(client):
    c=campaign(client)
    body={**c,'subject':'Subject\nBcc:someone@else.example'}
    assert client.put(f'/api/campaigns/{c["id"]}',json=body).status_code==422
    body={**c,'reply_mode':'automatic','auto_reply_body':''}
    assert client.put(f'/api/campaigns/{c["id"]}',json=body).status_code==422

def test_csv_formula_injection_prevented(client):
    c=campaign(client)
    client.post(f'/api/contacts/import/{c["id"]}',json={'contacts':[{'email':'hello@company.example','name':'=1+1','company':'@TEST'}]})
    exported=client.get('/api/export/contacts').text
    assert "'=1+1" in exported and "'@TEST" in exported

def test_billing_not_configured_is_not_fake_checkout(client):
    with patch.dict('os.environ',{},clear=True):
        assert client.get('/api/billing').json()['configured'] is False
        assert client.post('/api/billing/checkout/starter').status_code==503

def test_product_assets_exist(client):
    assert client.get('/').status_code==200
    js=client.get('/static/app.js');assert js.status_code==200 and 'boot()' in js.text
    assert client.get('/static/style.css').status_code==200


def test_concurrent_reply_submissions_create_one_job(app,client):
    from concurrent.futures import ThreadPoolExecutor
    c=campaign(client);person=contact(client,c['id']);mid=inbound(app,client,person)
    cookie=client.cookies.get('egasis_session')
    def submit(_):
        with TestClient(app) as caller:
            caller.cookies.set('egasis_session',cookie)
            return caller.post(f'/api/messages/{mid}/reply',json={'subject':'Re: Consulta','body':'Respuesta aprobada para Ana.'})
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,range(2)))
    assert [r.status_code for r in results]==[200,200]
    assert results[0].json()['id']==results[1].json()['id']
    with app.state.factory() as db:assert len(db.scalars(select(Job)).all())==1


def test_manual_confirmation_respects_pending_public_reservation(app,client):
    from egasis.models import Meeting
    c=campaign(client);person=contact(client,c['id']);slot=time.time()+7200
    with app.state.factory() as db:
        db.add(Meeting(workspace_id=person['workspace_id'],contact_id=person['id'],starts_at=slot,status='reserving'));db.commit()
    proposed=client.post('/api/meetings',json={'contact_id':person['id'],'starts_at':slot}).json()
    assert client.post(f'/api/meetings/{proposed["id"]}/confirm').status_code==409


def test_cancelled_meeting_cannot_be_reconfirmed(client):
    c=campaign(client);person=contact(client,c['id'])
    proposed=client.post('/api/meetings',json={'contact_id':person['id'],'starts_at':time.time()+7200}).json()
    assert client.post(f'/api/meetings/{proposed["id"]}/cancel').status_code==200
    assert client.post(f'/api/meetings/{proposed["id"]}/confirm').status_code==409


def test_metrics_distinguish_automatic_notices_from_replies(app,client):
    c=campaign(client);person=contact(client,c['id'])
    with app.state.factory() as db:
        for classification in ['question','bounce','out_of_office']:
            db.add(Message(workspace_id=person['workspace_id'],contact_id=person['id'],direction='inbound',subject='Notice',body='Fixture',classification=classification))
        db.commit()
    metrics=client.get('/api/metrics').json()
    assert metrics['received']==3 and metrics['replies']==1 and metrics['bounces']==1


def test_reviewed_knowledge_is_scoped_and_owner_controlled(app,client):
    from egasis.knowledge import KnowledgeService
    c=campaign(client);person=contact(client,c['id']);mid=inbound(app,client,person)
    wid=person['workspace_id'];service=KnowledgeService(app.state.factory)
    result=client.post('/api/knowledge',json={'text':'Realizamos visitas de diagnóstico antes de cotizar.','source_message_id':mid})
    assert result.status_code==200,result.text
    note=result.json();assert note['status']=='draft' and service.context(wid)==[]
    with TestClient(app) as other:
        other.post('/api/register',json={'name':'Otro negocio','email':'learning@other.example','password':'A-long-test-password'})
        assert other.get('/api/knowledge').json()==[]
        assert other.post(f'/api/knowledge/{note["id"]}/approve').status_code==404
        assert other.post('/api/knowledge',json={'text':'Referencia a una conversación de otro cliente.','source_message_id':mid}).status_code==404
    assert client.post(f'/api/knowledge/{note["id"]}/approve').status_code==200
    assert service.context(wid)[0]['text']==note['text']
    assert client.post(f'/api/knowledge/{note["id"]}/archive').status_code==200
    assert service.context(wid)==[]
    assert client.post(f'/api/knowledge/{note["id"]}/approve').status_code==409
    with app.state.factory() as db:
        owner=db.scalar(select(User).where(User.workspace_id==wid));owner.role='viewer';db.commit()
    assert client.get('/api/knowledge').status_code==200
    assert client.post('/api/knowledge',json={'text':'Un lector no puede agregar una condición comercial.'}).status_code==403


def test_campaign_results_attribute_costs_and_confirmed_outcomes(app,client):
    from egasis.models import Meeting, Usage
    first=campaign(client);person=contact(client,first['id']);second=campaign(client,name='Otra campaña')
    contact(client,first['id'],email='pending@company.example')
    second_person=contact(client,second['id'],email='second@company.example')
    second_other=contact(client,second['id'],email='third@company.example')
    with TestClient(app) as other:
        result=other.post('/api/register',json={'name':'Otro negocio','email':'metrics@other.example','password':'A-long-test-password'})
        assert result.status_code==200
        foreign_campaign=campaign(other);foreign_person=contact(other,foreign_campaign['id'])
        with app.state.factory() as db:
            db.get(Contact,foreign_person['id']).status='interested';db.commit()
        foreign_metrics=other.get('/api/metrics').json()
        assert foreign_metrics['interested']==1
        assert [(r['id'],r['interested']) for r in foreign_metrics['campaign_results']]==[(foreign_campaign['id'],1)]
    with app.state.factory() as db:
        for interested in [person,second_person,second_other]:
            db.get(Contact,interested['id']).status='interested'
        db.add(Usage(workspace_id=person['workspace_id'],contact_id=person['id'],operation='research',model='fixture',cost=.12))
        db.add(Usage(workspace_id=person['workspace_id'],contact_id=person['id'],operation='research',model='fixture',cost=2,created_at=time.time()-90000))
        db.add(Meeting(workspace_id=person['workspace_id'],contact_id=person['id'],starts_at=time.time()+3600,status='proposed'))
        db.add(Message(workspace_id=person['workspace_id'],contact_id=person['id'],direction='outbound',subject='Test',body='Fixture',status='simulated'))
        db.commit()
    metrics=client.get('/api/metrics').json()
    rows={r['id']:r for r in metrics['campaign_results']}
    assert set(rows)=={first['id'],second['id']}
    assert metrics['interested']==3
    assert rows[first['id']]['interested']==1 and rows[second['id']]['interested']==2
    assert rows[first['id']]['cost_24h']==.12 and rows[second['id']]['cost_24h']==0
    assert rows[first['id']]['sent']==0 and rows[first['id']]['simulated']==1 and rows[first['id']]['meetings']==0
