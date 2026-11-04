"""Synthetic feed payloads; real-feed validation is performed separately."""
import copy
import json
from datetime import timedelta

import httpx
import pytest
from backend import main,public_data
from tests.test_foundation import client
from tests.test_models import synthetic_rows

NOW=main.datetime(2026,11,2,12,tzinfo=main.timezone.utc)
URL=public_data.BASE+'/2026-27/en.1.json'


def dataset(payload=None,at=NOW):
    return {'url':URL,'retrieved_at':at.isoformat(),'payload':payload or {'name':'English Premier League 2026/27','matches':[
        {'date':'2026-09-01','time':'15:00','team1':'Synthetic Home','team2':'Synthetic Away','score':{'ft':[2,1],'ht':[1,0]}},
        {'date':'2026-09-02','time':'15:00','team1':'Synthetic Away','team2':'Synthetic Home','score':[0,0]},
        {'date':'2026-11-05','time':'20:00','team1':'Synthetic Home','team2':'Synthetic Away'}]}}


def test_documented_score_variants_and_timezone_assumption():
    parsed=public_data.parse_dataset(dataset(),NOW,7)
    assert len(parsed['records'])==2
    assert parsed['records'][0]['metrics']['goals_home']==2
    assert parsed['records'][1]['metrics']['goals_home']==0
    assert parsed['records'][1]['metrics']['ht_goals_home'] is None
    assert parsed['records'][0]['metrics']['xg_home'] is None
    assert parsed['records'][0]['kickoff'].endswith('14:00:00+00:00')  # UK summer.
    listed=next(f for f in parsed['fixtures'] if f['status']=='listed')
    assert listed['kickoff_utc'].endswith('20:00:00+00:00')  # UK winter.


@pytest.mark.parametrize('change',[{'score':{'ft':[-1,0]}},{'score':{'ft':['2',0]}},{'score':{'ft':[1,0],'ht':[2,0]}},
    {'score':'not a score'},{'team1':'Synthetic Away'},{'date':'2099-01-01'}])
def test_invalid_source_rows_rejected(change):
    data=dataset();data['payload']['matches'][0].update(change)
    with pytest.raises(public_data.FeedError):public_data.parse_dataset(data,NOW,7)


def test_missing_time_is_not_invented():
    data=dataset();data['payload']['matches'][0].pop('time')
    parsed=public_data.parse_dataset(data,NOW,7)
    assert parsed['missing_times']==1 and len(parsed['records'])==1


def test_fixed_destination_no_api_credentials_and_cache_revalidation(tmp_path,monkeypatch):
    monkeypatch.setenv('API_FOOTBALL_KEY','synthetic-secret-not-for-github')
    calls=[]
    def respond(request):
        assert str(request.url)==URL
        assert 'x-apisports-key' not in request.headers
        assert 'synthetic-secret-not-for-github' not in str(request.headers)
        calls.append(request)
        return httpx.Response(304) if 'if-none-match' in request.headers else httpx.Response(200,json=dataset()['payload'],headers={'etag':'test-etag'})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        first=public_data.download(client,'2026-27',tmp_path,NOW)
        second=public_data.download(client,'2026-27',tmp_path,NOW+timedelta(days=1))
    assert len(calls)==2 and second['cache_status']=='revalidated'
    assert first['retrieved_at']==second['retrieved_at']


def test_invalid_new_payload_preserves_last_valid_cache(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=dataset()['payload']))) as client:
        first=public_data.download(client,'2026-27',tmp_path,NOW)
    bad=dataset()['payload'];bad['matches'][0]['score']={'ft':[-5,0]}
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=bad))) as client:
        second=public_data.download(client,'2026-27',tmp_path,NOW+timedelta(hours=1))
    assert second['cache_status']=='offline_cache' and second['sha256']==first['sha256']
    assert 'invalid' in second['warning']


def test_invalid_seasons_cannot_make_arbitrary_requests(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda r:pytest.fail('Invalid input reached HTTP'))) as client:
        with pytest.raises(public_data.FeedError):public_data.fetch(['../../secrets'],tmp_path,NOW,7,client)


def test_unchanged_results_do_not_rewrite_receipts_and_corrections_append(client):
    first=public_data.parse_dataset(dataset(),NOW,7)
    with main.db() as c:
        assert public_data.ingest_history(c,first['records'],NOW)['inserted']==2
        before=c.execute('SELECT id,imported_at FROM historical_observations ORDER BY id').fetchall()
        later=public_data.parse_dataset(dataset(at=NOW+timedelta(days=1)),NOW+timedelta(days=1),7)
        assert public_data.ingest_history(c,later['records'],NOW+timedelta(days=1))['unchanged']==2
        after=c.execute('SELECT id,imported_at FROM historical_observations ORDER BY id').fetchall()
        assert [tuple(r) for r in before]==[tuple(r) for r in after]
        corrected=dataset(at=NOW+timedelta(days=1));corrected['payload']['matches'][0]['score']['ft']=[3,1]
        parsed=public_data.parse_dataset(corrected,NOW+timedelta(days=1),7)
        assert public_data.ingest_history(c,parsed['records'],NOW+timedelta(days=1))['inserted']==1


def test_public_listing_research_only_and_manual_status_preserved(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW+timedelta(days=3))
    parsed=public_data.parse_dataset(dataset(),NOW,7)
    with main.db() as c:
        public_data.ingest_fixtures(c,parsed['fixtures'],NOW)
    listed=next(f for f in main.fixtures() if f['status']=='listed')
    assert listed['research_eligible'] and not listed['eligible']
    csv='home,away,competition,kickoff_sast,status\n'+f"{listed['home']},{listed['away']},{listed['competition']},{listed['kickoff_sast']},cancelled"
    client.post('/api/fixtures/import',json={'csv':csv})
    with main.db() as c:
        assert c.execute('SELECT source FROM fixtures WHERE id=?',(listed['id'],)).fetchone()[0]=='Manual CSV · unverified'
        public_data.ingest_fixtures(c,parsed['fixtures'],NOW+timedelta(hours=1))
        assert c.execute('SELECT status FROM fixtures WHERE id=?',(listed['id'],)).fetchone()[0]=='cancelled'


def synthetic_feed():
    matches=[]
    for row in synthetic_rows(NOW+timedelta(days=120))[:60]:
        kickoff=main.datetime.fromisoformat(row['kickoff_utc']).astimezone(public_data.ZoneInfo('Europe/London'))
        matches.append({'date':kickoff.date().isoformat(),'time':kickoff.strftime('%H:%M'),'team1':row['home'],'team2':row['away'],
                        'score':{'ft':[row['metrics']['goals_home'],row['metrics']['goals_away']]}})
    matches.append({'date':'2026-11-02','time':'15:00','team1':'Synthetic 0','team2':'Synthetic 1'})
    parsed=public_data.parse_dataset(dataset({'name':'English Premier League 2026/27','matches':matches}),NOW,7)
    return [{'data':{'url':URL,'sha256':'synthetic','retrieved_at':NOW.isoformat(),'cache_status':'test_fixture'},**parsed}]


def test_one_button_workflow_fits_models_without_fake_missing_stats(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    monkeypatch.setattr(public_data,'fetch',lambda *args:synthetic_feed())
    result=client.post('/api/workflow/run',json={})
    assert result.status_code==200
    output=result.json()
    assert output['status']=='research_working' and output['models_generated']==1
    assert output['history_inserted']==60
    assert output['latest_reported_result_date']=='2026-11-01'
    run=client.get('/api/analysis/'+str(output['analyses'][0]['run_id'])).json()
    assert 'poisson' in run['models'] and 'xg_poisson' not in run['models']
    assert client.get('/api/dashboard').json()['qualified']==0
    second=client.post('/api/workflow/run',json={}).json()
    assert second['history_inserted']==0 and second['history_unchanged']==60


def test_workflow_network_failure_releases_lock_and_does_not_write_fake_results(client,monkeypatch):
    def failure(*args):raise public_data.FeedError('Synthetic network failure')
    monkeypatch.setattr(public_data,'fetch',failure)
    assert client.post('/api/workflow/run',json={}).status_code==502
    assert not main.WORKFLOW_LOCK.locked()
    assert client.get('/api/history').json()['total']==0


def test_key_presence_never_exposes_value(client,monkeypatch):
    key='synthetic-private-key'
    monkeypatch.setenv('API_FOOTBALL_KEY',key)
    status=client.get('/api/workflow/status')
    assert status.json()['api_football_key_present']
    assert key not in status.text and key not in client.get('/api/providers').text
