import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from egasis.app import create_app
from egasis.models import Campaign,Connection,Contact,Meeting,Workspace
from egasis.security import Vault
from egasis.settings import Settings
from egasis.booking import BookingService,BookingError

@pytest.fixture
def setup(tmp_path):
    settings=Settings('sqlite:///'+str(tmp_path/'booking.db'),'booking-test-key')
    app=create_app(settings);factory=app.state.factory;vault=app.state.vault
    with factory() as db:
        workspace=Workspace(name='Estudio Norte',timezone='UTC');db.add(workspace);db.flush()
        campaign=Campaign(workspace_id=workspace.id,name='Diseño',subject='Hello',body='Hi',weekdays='0,1,2,3,4,5,6',start_hour=0,end_hour=24);db.add(campaign);db.flush()
        contacts=[Contact(workspace_id=workspace.id,campaign_id=campaign.id,email=email,name='Ana') for email in ['a@example.com','b@example.com']];db.add_all(contacts);db.commit()
        wid=workspace.id;cids=[c.id for c in contacts]
    yield app,BookingService(factory,vault),wid,cids

def test_signed_invitation_returns_slots_without_disclosing_other_contacts(setup):
    app,service,wid,cids=setup;token=app.state.vault.booking_token(wid,cids[0])
    result=service.availability(wid,cids[0],token)
    assert len(result['slots'])>0 and 'email' not in result
    assert result['business']=='Estudio Norte'
    with pytest.raises(BookingError):service.availability(wid,cids[1],token)
    with pytest.raises(BookingError):service.availability(wid,cids[0],app.state.vault.booking_token(wid,cids[0],int(time.time())-1))

def test_recipient_confirmation_is_persisted_and_repeat_is_idempotent(setup):
    app,service,wid,cids=setup;token=app.state.vault.booking_token(wid,cids[0]);slot=service.availability(wid,cids[0],token)['slots'][0]
    result=service.reserve(wid,cids[0],token,slot)
    assert result['status']=='confirmed'
    assert service.reserve(wid,cids[0],token,slot)['id']==result['id']
    with app.state.factory() as db:assert len(db.scalars(select(Meeting)).all())==1

def test_two_contacts_cannot_book_same_slot(setup):
    app,service,wid,cids=setup;tokens=[app.state.vault.booking_token(wid,cid) for cid in cids];slot=service.availability(wid,cids[0],tokens[0])['slots'][0]
    def book(index):
        try:return service.reserve(wid,cids[index],tokens[index],slot)['status']
        except BookingError:return 'unavailable'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(book,[0,1]))
    assert sorted(results)==['confirmed','unavailable']

def test_google_busy_intervals_are_respected_and_errors_do_not_make_slots_free(setup):
    app,service,wid,cids=setup;token=app.state.vault.booking_token(wid,cids[0]);slot=service.availability(wid,cids[0],token)['slots'][0]
    with app.state.factory() as db:db.add(Connection(workspace_id=wid,provider='google_calendar',secret='unused'));db.commit()
    from datetime import datetime,timezone
    start=datetime.fromtimestamp(slot,timezone.utc).isoformat();end=datetime.fromtimestamp(slot+1800,timezone.utc).isoformat()
    with patch('egasis.calendar_service.CalendarService.availability',return_value={'busy':[{'start':start,'end':end}]}):
        assert slot not in service.availability(wid,cids[0],token)['slots']
    with patch('egasis.calendar_service.CalendarService.availability',side_effect=RuntimeError('unavailable')):
        with pytest.raises(RuntimeError):service.availability(wid,cids[0],token)

def test_public_booking_api_and_page(setup):
    app,service,wid,cids=setup;token=app.state.vault.booking_token(wid,cids[0]);params={'workspace_id':wid,'contact_id':cids[0],'token':token}
    with TestClient(app) as client:
        assert client.get('/book').status_code==200
        result=client.get('/api/booking',params=params);assert result.status_code==200
        data={**params,'starts_at':result.json()['slots'][0]}
        assert client.post('/api/booking',json=data).json()['status']=='confirmed'
        data['token']='forged-token-long-enough';assert client.post('/api/booking',json=data).status_code==409

def test_calendar_ambiguity_leaves_slot_reserved_for_review(setup):
    app,service,wid,cids=setup;token=app.state.vault.booking_token(wid,cids[0]);slot=service.availability(wid,cids[0],token)['slots'][0]
    with app.state.factory() as db:db.add(Connection(workspace_id=wid,provider='google_calendar',secret='unused'));db.commit()
    with patch('egasis.calendar_service.CalendarService.availability',return_value={'busy':[]}),patch('egasis.calendar_service.CalendarService.create_event',side_effect=RuntimeError('lost acknowledgement')):
        with pytest.raises(BookingError):service.reserve(wid,cids[0],token,slot)
        assert slot not in service.availability(wid,cids[0],token)['slots']
    with app.state.factory() as db:assert db.scalar(select(Meeting)).status=='reserving'


def test_reopening_invitation_shows_existing_confirmation(setup):
    app,service,wid,cids=setup;token=app.state.vault.booking_token(wid,cids[0])
    slot=service.availability(wid,cids[0],token)['slots'][0]
    service.reserve(wid,cids[0],token,slot)
    reopened=service.availability(wid,cids[0],token)
    assert reopened['slots']==[] and reopened['reservation']=={'status':'confirmed','starts_at':slot}
