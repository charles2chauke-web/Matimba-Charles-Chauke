"""Synthetic fixtures: these are software checks, never real predictions."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from backend import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    monkeypatch.delenv('CHARLES_CREDENTIAL_KEY', raising=False)
    monkeypatch.delenv('API_FOOTBALL_KEY', raising=False)
    with TestClient(main.app) as client:
        yield client


HEADER = 'home,away,competition,kickoff_sast,status\n'


def content(status='scheduled', home='Synthetic Home', offset=True):
    kickoff = (main.now().astimezone(main.SAST) + timedelta(minutes=15)).replace(microsecond=0)
    if not offset:
        kickoff = kickoff.replace(tzinfo=None)
    return HEADER + f'{home},Synthetic Away,Synthetic League,{kickoff.isoformat()},{status}\n'


def test_health_and_honest_empty_state(client):
    assert client.get('/api/health').json()['status'] == 'ok'
    result = client.get('/api/dashboard').json()
    assert result['selections'] == []
    assert result['historical_roi'] is None
    assert result['average_probability'] is None


def test_import_duplicate_and_status_update(client):
    csv = content()
    assert client.post('/api/fixtures/import', json={'csv': csv}).json()['inserted'] == 1
    assert client.post('/api/fixtures/import', json={'csv': csv.replace('Synthetic Home',' synthetic   HOME ')}).json()['unchanged'] == 1
    result = client.post('/api/fixtures/import', json={'csv': csv.replace('scheduled','postponed')}).json()
    assert result['updated'] == 1
    fixture = client.get('/api/fixtures').json()[0]
    assert not fixture['eligible']
    assert fixture['reliability'] == 'Unverified manual import'


@pytest.mark.parametrize('status', ['live','completed','postponed','cancelled'])
def test_excludes_non_prematch_status(client, status):
    client.post('/api/fixtures/import', json={'csv':content(status)})
    assert client.get('/api/dashboard').json()['upcoming'] == 0


def test_sast_conversion(client):
    assert client.post('/api/fixtures/import',json={'csv':content(offset=False)}).status_code == 200
    fixture = client.get('/api/fixtures').json()[0]
    assert fixture['kickoff_sast'].endswith('+02:00')
    assert fixture['kickoff_utc'].endswith('+00:00')


@pytest.mark.parametrize('csv',[HEADER+'A,B,L,invalid,scheduled',HEADER+'A,B,L,2027-01-01T12:00:00Z,scheduled',HEADER+'A,A,L,2027-01-01T12:00:00,scheduled',HEADER+'A,B,L,2027-01-01T12:00:00,bad','wrong,header\nA,B',HEADER])
def test_bad_csv_rejected(client,csv):
    assert client.post('/api/fixtures/import',json={'csv':csv}).status_code == 422
    assert client.get('/api/fixtures').json() == []


def test_atomic_import(client):
    csv=content()+'Bad,Bad,L,invalid,scheduled\n'
    assert client.post('/api/fixtures/import',json={'csv':csv}).status_code == 422
    assert client.get('/api/fixtures').json() == []


def test_past_match_not_eligible(client):
    csv=HEADER+'Synthetic A,Synthetic B,Synthetic League,2020-01-01T19:00:00,scheduled'
    client.post('/api/fixtures/import',json={'csv':csv})
    assert not client.get('/api/fixtures').json()[0]['eligible']


def test_credentials_encrypted_not_exposed(client):
    secret='synthetic-test-secret-not-a-real-key'
    assert client.put('/api/providers/api-football',json={'key':secret}).status_code == 200
    data=client.get('/api/providers').json()
    assert secret not in json.dumps(data)
    assert data[0]['configured']
    with main.db() as connection:
        stored=connection.execute('SELECT encrypted_key FROM providers').fetchone()[0]
    assert stored != secret
    assert main.credential_cipher().decrypt(stored.encode()).decode() == secret
    assert client.delete('/api/providers/api-football').status_code == 200
    assert not client.get('/api/providers').json()[0]['configured']


def test_validation_does_not_echo_credentials(client):
    secret='sensitive'*100
    response=client.put('/api/providers/api-football',json={'key':secret})
    assert response.status_code == 422
    assert secret not in response.text


def test_settings_persist_and_validate(client):
    settings=client.get('/api/settings').json()
    settings['window']='24h'
    assert client.put('/api/settings',json=settings).status_code == 422
    settings['window']='today'
    settings['min_probability']=.8
    assert client.put('/api/settings',json=settings).status_code == 200
    assert client.get('/api/settings').json()['min_probability'] == .8
    settings['min_probability']=1.1
    assert client.put('/api/settings',json=settings).status_code == 422


def test_reject_cross_origin_and_bad_host(client):
    assert client.get('/api/providers',headers={'Origin':'https://evil.example'}).status_code == 403
    assert client.get('/api/health',headers={'Host':'evil.example'}).status_code == 400
    assert client.post('/api/fixtures/import',content='csv=test',headers={'Content-Type':'application/x-www-form-urlencoded'}).status_code == 415


def test_restart_preserves_data(client):
    client.post('/api/fixtures/import',json={'csv':content()})
    main.initialize()
    assert len(client.get('/api/fixtures').json()) == 1
