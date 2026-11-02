"""Synthetic provider responses verify the adapter, not live account access."""
import json
from datetime import timedelta

import httpx
import pytest
from backend import api_football as provider, main, history
from tests.test_foundation import client

KEY='synthetic-direct-api-key-not-real'
NOW=main.datetime(2026,11,2,12,tzinfo=main.timezone.utc)


@pytest.fixture(autouse=True)
def reset_rate_state(monkeypatch):
    monkeypatch.setattr(provider,'_next_request',0)
    monkeypatch.setattr(provider,'_cooldown_until',0)
    monkeypatch.setattr(provider,'_daily_remaining',None)
    monkeypatch.setattr(provider.time,'sleep',lambda _:None)


def account():
    return {'get':'status','errors':[],'results':1,'response':{
        'account':{'email':'do-not-return@example.invalid','firstname':'Private'},
        'subscription':{'plan':'Pro','active':True,'end':'2099-12-01T00:00:00+00:00'},
        'requests':{'current':2,'limit_day':7500}}}


def envelope(endpoint,rows):
    return {'get':endpoint,'errors':[],'results':len(rows),'paging':{'current':1,'total':1},'response':rows}


def league():
    return {'league':{'id':39,'name':'Synthetic League'},'country':{'name':'Synthetic Country'},
            'seasons':[{'year':2026,'coverage':{'odds':True,'injuries':False}}]}


def fixture(number=1,status='NS',kickoff=None,home=1,away=2,score=(2,1)):
    return {'fixture':{'id':number,'date':(kickoff or NOW+timedelta(hours=3)).isoformat(),'status':{'short':status}},
            'league':{'id':39,'season':2026},
            'teams':{'home':{'id':home,'name':f'Synthetic Team {home}'},'away':{'id':away,'name':f'Synthetic Team {away}'}},
            'score':{'fulltime':{'home':score[0],'away':score[1]},'halftime':{'home':None,'away':None}}}


def wire(monkeypatch,handler):
    monkeypatch.setattr(provider,'client',lambda:httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=False))


def save(client):
    assert client.put('/api/providers/api-football',json={'key':KEY}).status_code==200


def standard(rows):
    def handler(request):
        assert request.url.host=='v3.football.api-sports.io'
        assert request.headers['x-apisports-key']==KEY
        assert KEY not in str(request.url)
        if request.url.path=='/status':return httpx.Response(200,json=account())
        if request.url.path=='/leagues':return httpx.Response(200,json=envelope('leagues',[league()]))
        assert request.url.path=='/fixtures'
        return httpx.Response(200,json=envelope('fixtures',rows))
    return handler


def run(client):
    return client.post('/api/workflow/run',json={'source':'api-football','api_seasons':[2026]})


def test_connection_without_key_never_calls_network(client,monkeypatch):
    wire(monkeypatch,lambda _:pytest.fail('No key must not make a request'))
    assert client.post('/api/providers/api-football/test',json={}).status_code==409


def test_success_reports_plan_quota_without_personal_data_or_key(client,monkeypatch):
    save(client);wire(monkeypatch,standard([]))
    response=client.post('/api/providers/api-football/test',json={})
    assert response.status_code==200
    assert response.json()['ok']
    assert response.json()['usage']['daily_remaining']==7498
    report=client.get('/api/providers').json()[0]
    assert report['last_check']['subscription']['plan']=='Pro'
    assert KEY not in json.dumps(report)
    assert 'do-not-return' not in response.text
    with main.db() as connection:
        assert KEY not in connection.execute('SELECT result FROM provider_checks').fetchone()[0]
    client.put('/api/providers/api-football',json={'key':'different-synthetic-key'})
    assert client.get('/api/providers').json()[0]['last_check'] is None


@pytest.mark.parametrize('status,code',[(401,'access_denied'),(403,'access_denied'),(429,'rate_limited'),(499,'provider_unavailable'),(500,'provider_unavailable'),(302,'provider_unavailable')])
def test_http_failures_safe_and_no_retry_or_redirect(client,monkeypatch,status,code):
    save(client);calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,headers={'location':'https://other.invalid/'+KEY,'retry-after':'120'},text=KEY)
    wire(monkeypatch,handler)
    result=client.post('/api/providers/api-football/test',json={}).json()
    assert not result['ok'] and result['code']==code
    assert KEY not in json.dumps(result)
    assert len(calls)==1
    assert client.get('/api/providers').json()[0]['configured']
    if status==429:
        second=client.post('/api/providers/api-football/test',json={}).json()
        assert second['code']=='rate_limited' and len(calls)==1


@pytest.mark.parametrize('errors,code',[({'token':KEY},'authentication_failed'),({'requests':KEY},'quota_or_rate_limit'),({'plan':KEY},'provider_rejected'),([KEY],'provider_rejected')])
def test_http_200_error_envelopes_not_success_or_secret_echo(client,monkeypatch,errors,code):
    save(client)
    wire(monkeypatch,lambda _:httpx.Response(200,json={**account(),'errors':errors}))
    result=client.post('/api/providers/api-football/test',json={}).json()
    assert result['code']==code and not result['ok']
    assert KEY not in json.dumps(result)


def test_timeout_safe_and_saved_failure(client,monkeypatch):
    save(client)
    def handler(request):raise httpx.ReadTimeout(KEY,request=request)
    wire(monkeypatch,handler)
    result=client.post('/api/providers/api-football/test',json={})
    assert result.json()['code']=='timeout' and KEY not in result.text
    assert not client.get('/api/providers').json()[0]['last_check']['ok']


@pytest.mark.parametrize('payload',[[],{'errors':[]}, {**account(),'response':{}}, {**account(),'errors':'invalid'}])
def test_malformed_account_response_safe(client,monkeypatch,payload):
    save(client);wire(monkeypatch,lambda _:httpx.Response(200,json=payload))
    assert client.post('/api/providers/api-football/test',json={}).json()['code']=='invalid_response'


def test_inactive_subscription_and_unsupported_provider(client,monkeypatch):
    save(client);payload=account();payload['response']['subscription']['active']=False
    wire(monkeypatch,lambda _:httpx.Response(200,json=payload))
    assert not client.post('/api/providers/api-football/test',json={}).json()['ok']
    assert run(client).status_code==409
    assert client.post('/api/providers/sportmonks/test',json={}).status_code==409


def test_env_key_supported_and_local_key_preferred(client,monkeypatch):
    monkeypatch.setenv('API_FOOTBALL_KEY',KEY)
    wire(monkeypatch,standard([]))
    assert client.post('/api/providers/api-football/test',json={}).json()['ok']
    monkeypatch.setenv('API_FOOTBALL_KEY','different-env-key')
    assert client.get('/api/providers').json()[0]['last_check'] is None
    save(client)
    assert client.post('/api/providers/api-football/test',json={}).json()['ok']


def test_import_idempotent_and_correction_append_only(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    save(client)
    rows=[fixture(1,'FT',NOW-timedelta(days=1)),fixture(2,'PST',NOW+timedelta(days=1)),
          fixture(3,'TBD',NOW+timedelta(days=2)),fixture(4)]
    wire(monkeypatch,standard(rows))
    first=run(client)
    assert first.status_code==200,first.text
    assert first.json()['history_inserted']==1 and first.json()['fixtures_inserted']==4
    ids=[f['id'] for f in client.get('/api/fixtures').json()]
    assert run(client).json()['history_inserted']==0
    assert [f['id'] for f in client.get('/api/fixtures').json()]==ids
    history_before=client.get('/api/history').json()['items'][0]
    assert history_before['metrics']['xg_home'] is None
    assert history_before['metrics']['ht_goals_home'] is None
    assert history_before['metrics']['goals_home']==2
    rows[0]['score']['fulltime']['home']=3
    monkeypatch.setattr(main,'now',lambda:NOW+timedelta(minutes=1))
    assert run(client).json()['history_inserted']==1
    with main.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM historical_observations').fetchone()[0]==2
        mid=connection.execute('SELECT id FROM historical_matches').fetchone()[0]
        assert history.resolve(history.observations(connection,mid,NOW.isoformat()))['metrics']['goals_home']==2
    assert client.get('/api/history').json()['items'][0]['metrics']['goals_home']==3


def test_reschedule_preserves_provider_id_and_old_observations(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    save(client);rows=[fixture()];wire(monkeypatch,standard(rows))
    assert run(client).status_code==200
    before=client.get('/api/fixtures').json()[0]
    rows[0]['fixture']['date']=(NOW+timedelta(days=2)).isoformat()
    rows[0]['teams']['home']['name']='Renamed display name'
    assert run(client).status_code==200
    after=client.get('/api/fixtures').json()[0]
    assert after['id']==before['id'] and after['home']==before['home']
    assert after['kickoff_utc']!=before['kickoff_utc']
    with main.db() as connection:
        assert connection.execute('SELECT COUNT(DISTINCT identity) FROM fixture_observations').fetchone()[0]==2


def test_bad_later_fixture_rolls_back_entire_import(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    save(client);bad=fixture(2);bad['fixture']['date']='not-a-date'
    wire(monkeypatch,standard([fixture(),bad]))
    assert run(client).status_code==502
    assert client.get('/api/fixtures').json()==[]
    assert client.get('/api/history').json()['total']==0


def test_provider_identity_conflict_rolls_back_updates(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    save(client);rows=[fixture()];wire(monkeypatch,standard(rows))
    assert run(client).status_code==200
    before=client.get('/api/fixtures').json()
    rows[0]['teams']['home']['id']=88
    assert run(client).status_code==502
    assert client.get('/api/fixtures').json()==before


@pytest.mark.parametrize('paging',[{'current':1,'total':2},{'current':2,'total':2},None])
def test_incomplete_paging_refused_without_import(client,monkeypatch,paging):
    save(client)
    base=standard([])
    def handler(request):
        if request.url.path=='/fixtures':return httpx.Response(200,json={**envelope('fixtures',[fixture()]),'paging':paging})
        return base(request)
    wire(monkeypatch,handler)
    assert run(client).status_code==502
    assert client.get('/api/fixtures').json()==[]


def test_no_coverage_not_silently_public_fallback(client,monkeypatch):
    save(client);base=standard([])
    wire(monkeypatch,lambda r:httpx.Response(200,json=envelope('leagues',[])) if r.url.path=='/leagues' else base(r))
    assert run(client).status_code==502
    assert client.get('/api/workflow/status').json()['last_run'] is None


def test_rate_limit_headers_and_oversize_response(client,monkeypatch):
    save(client);calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=account(),headers={'X-RateLimit-Remaining':'0'})
    wire(monkeypatch,handler)
    assert client.post('/api/providers/api-football/test',json={}).json()['ok']
    assert client.post('/api/providers/api-football/test',json={}).json()['code']=='rate_limited'
    assert len(calls)==1
    monkeypatch.setattr(provider,'_next_request',0)
    monkeypatch.setattr(provider,'_cooldown_until',0)
    wire(monkeypatch,lambda _:httpx.Response(200,content=b'x'*4_000_001))
    assert client.post('/api/providers/api-football/test',json={}).json()['code']=='invalid_response'


def test_auto_uses_key_and_fits_real_algorithm_on_synthetic_results(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    save(client)
    rows=[]
    for i in range(90):
        home=i%6+1;away=(i//6+i+1)%6+1
        if away==home:away=away%6+1
        rows.append(fixture(100+i,'FT',NOW-timedelta(days=91-i),home,away,(i%4,(i//3)%3)))
    rows.append(fixture(500))
    wire(monkeypatch,standard(rows))
    response=client.post('/api/workflow/run',json={'api_seasons':[2026]})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['source'].startswith('API-Football') and result['history_inserted']==90
    assert result['models_generated']==1,result
    report=client.get('/api/analysis/'+str(result['analyses'][0]['run_id'])).json()
    assert set(report['models'])=={'poisson','dixon_coles'}
    assert report['status']=='experimental'
    assert report['models']['poisson']['result']['home']>0
    assert client.get('/api/dashboard').json()['qualified']==0


def test_extra_time_awarded_unknown_and_naive_dates_excluded():
    for code in ('AET','PEN','AWD','WO'):
        parsed=provider.parse_fixture(fixture(status=code,kickoff=NOW-timedelta(days=1)),39,2026,NOW)
        assert parsed['metrics'] is None
    for change in ('status','date'):
        raw=fixture()
        if change=='status':raw['fixture']['status']['short']='UNKNOWN'
        else:raw['fixture']['date']='2026-11-02T18:00:00'
        with pytest.raises(provider.ProviderError):provider.parse_fixture(raw,39,2026,NOW)


def test_omitted_fixture_withheld_and_result_receipt_preserved(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    save(client);rows=[fixture(1,'FT',NOW-timedelta(days=1))];wire(monkeypatch,standard(rows))
    assert run(client).status_code==200
    rows.clear()
    monkeypatch.setattr(main,'now',lambda:NOW+timedelta(minutes=1))
    assert run(client).status_code==200
    assert client.get('/api/fixtures').json()[0]['status']=='unavailable'
    item=client.get('/api/history').json()['items'][0]
    assert item['metrics']['goals_home'] is None
    with main.db() as connection:
        assert history.resolve(history.observations(connection,item['id'],NOW.isoformat()))['metrics']['goals_home']==2


def test_known_exhausted_quota_blocks_data_call(client,monkeypatch):
    save(client);calls=[]
    payload=account();payload['response']['requests']['current']=7500
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200,json=payload)
    wire(monkeypatch,handler)
    assert run(client).status_code==502
    assert calls==['/status']


def test_duplicate_identity_is_not_silently_merged(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    save(client);wire(monkeypatch,standard([fixture(1),fixture(2)]))
    assert run(client).status_code==502
    assert client.get('/api/fixtures').json()==[]


def test_non_ascii_key_fails_safely_without_network(client,monkeypatch):
    client.put('/api/providers/api-football',json={'key':'synthetic-invalid-\u2603'})
    wire(monkeypatch,lambda _:pytest.fail('Invalid header characters must not reach HTTP transport'))
    assert client.post('/api/providers/api-football/test',json={}).json()['code']=='invalid_key_format'


def test_normal_low_minute_limit_spaces_calls_instead_of_aborting(client,monkeypatch):
    save(client);sleeps=[]
    monkeypatch.setattr(provider.time,'sleep',sleeps.append)
    wire(monkeypatch,lambda _:httpx.Response(200,json=account(),headers={'X-RateLimit-Limit':'10'}))
    assert client.post('/api/providers/api-football/test',json={}).json()['ok']
    assert client.post('/api/providers/api-football/test',json={}).json()['ok']
    assert any(5<delay<=6 for delay in sleeps)


def test_provider_lock_conflict_does_not_corrupt_workflow_lock(client):
    save(client)
    with provider.LOCK:
        assert client.post('/api/providers/api-football/test',json={}).status_code==409
        assert run(client).status_code==409
    assert not main.WORKFLOW_LOCK.locked()
