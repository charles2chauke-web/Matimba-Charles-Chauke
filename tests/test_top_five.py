"""Synthetic Top Five and provider quote cases; not actual betting evidence."""
import csv
import io
import json
from datetime import timedelta
import httpx
import pytest
from backend import main,api_football as provider,provider_odds,odds,history
from tests.test_foundation import client
from tests.test_models import fixture_and_history
from tests.test_api_football import NOW,KEY,fixture,envelope,account,wire,save,reset_rate_state


def raw_odds(update=None):
    return {'league':{'id':39,'season':2026},'fixture':{'id':1,'date':(NOW+timedelta(hours=3)).isoformat()},
      'update':(update or NOW-timedelta(minutes=10)).isoformat(),
      'bookmakers':[{'id':8,'name':'Synthetic Book','bets':[
        {'id':1,'name':'Match Winner','values':[{'value':'Home','odd':'2.10'},{'value':'Draw','odd':'3.50'},{'value':'Away','odd':'4.00'}]},
        {'id':5,'name':'Goals Over/Under','values':[{'value':'Under 3.5','odd':'1.50'},{'value':'Over 3.5','odd':'3.50'}]},
        {'id':8,'name':'Both Teams Score','values':[{'value':'Yes','odd':'2.10'}]},
        {'id':12,'name':'Double Chance','values':[{'value':'Home/Draw','odd':'1.40'}]},
        {'id':1,'name':'First Half Winner','values':[{'value':'Home','odd':'9.00'}]},
      ]}]}


def local_fixture():
    return {'id':1,'home':'Synthetic Home','away':'Synthetic Away','competition':'Synthetic League',
            'kickoff_utc':(NOW+timedelta(hours=3)).isoformat(),'status':'scheduled'}


def test_quote_mapping_timestamp_no_missing_or_wrong_period_invention():
    rows,_=provider_odds.parse([raw_odds()],{1:local_fixture()},39,2026,NOW)
    assert len(rows)==7
    assert all(r['observed_at']==(NOW-timedelta(minutes=10)).isoformat() for r in rows)
    assert all(r['imported_at']==NOW.isoformat() for r in rows)
    assert all(r['settlement']=='regulation_90' for r in rows)
    assert all(r['source_url'].endswith('/odds') for r in rows)
    assert [r['decimal_odds'] for r in rows if r['market']=='1x2' and r['selection']=='home']==[2.1]
    assert all(r['market']!='team_goals_home' for r in rows)


@pytest.mark.parametrize('change',['future','naive','rescheduled','nan','league','malformed'])
def test_bad_quote_metadata_rejected_atomically(change):
    raw=raw_odds()
    if change=='future':raw['update']=(NOW+timedelta(seconds=1)).isoformat()
    if change=='naive':raw['update']='2026-11-02T11:00:00'
    if change=='rescheduled':raw['fixture']['date']=(NOW+timedelta(days=1)).isoformat()
    if change=='nan':raw['bookmakers'][0]['bets'][0]['values'][0]['odd']='nan'
    if change=='league':raw['league']['id']=140
    if change=='malformed':raw['bookmakers']=None
    with pytest.raises(provider.ProviderError):provider_odds.parse([raw],{1:local_fixture()},39,2026,NOW)


def test_unknown_and_started_fixtures_not_priced():
    assert provider_odds.parse([raw_odds()],{},39,2026,NOW)[0]==[]
    f=local_fixture();f['status']='live'
    assert provider_odds.parse([raw_odds()],{1:f},39,2026,NOW)[0]==[]


def test_odds_pagination_complete_and_bounded():
    calls=[]
    def handler(request):
        page=int(request.url.params['page']);calls.append(page)
        return httpx.Response(200,json={**envelope('odds',[raw_odds()]),'paging':{'current':page,'total':2}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert len(provider_odds.pages(client,KEY,39,2026))==2
    assert calls==[1,2]
    def bad(request):return httpx.Response(200,json={**envelope('odds',[]),'paging':{'current':1,'total':6}})
    with httpx.Client(transport=httpx.MockTransport(bad)) as client:
        with pytest.raises(provider.ProviderError):provider_odds.pages(client,KEY,39,2026)


def test_daily_discovery_any_league_sast_midnight_and_no_tomorrow():
    # 22:30 UTC is already the next SAST day. Include a small non-preset league.
    current=NOW.replace(hour=22,minute=30)
    rows=[fixture(1,kickoff=current+timedelta(hours=1)),fixture(2,kickoff=current+timedelta(hours=24)),
          fixture(3,'PST',current+timedelta(hours=2)),fixture(4,kickoff=current-timedelta(minutes=1))]
    for r in rows:r['league']['id']=999
    def handler(request):
        assert request.url.params['date']=='2026-11-03'
        assert request.url.params['timezone']=='Africa/Johannesburg'
        return httpx.Response(200,json=envelope('fixtures',rows))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=provider.daily_leagues(client,KEY,current)
    assert len(result)==1 and result[0]['league_id']==999 and result[0]['fixture_ids']==[1]


def value_setup(client,monkeypatch,price_factor=1.10):
    monkeypatch.setattr(main,'now',lambda:NOW)
    fixture_and_history(client)
    with main.db() as c:
        f=dict(c.execute('SELECT * FROM fixtures WHERE id=1').fetchone())
        f['kickoff_utc']=(NOW+timedelta(hours=3)).isoformat()
        c.execute('UPDATE fixtures SET kickoff_utc=?,identity=?,source=? WHERE id=1',(f['kickoff_utc'],odds.fixture_identity(f),provider.SOURCE))
        c.execute('INSERT INTO api_football_fixtures VALUES (?,?,?,?,?,?,?)',(100,1,1,2,999,2026,NOW.isoformat()))
        c.execute('INSERT INTO top_five_daily_fixtures VALUES (?,?,?)',(100,NOW.astimezone(main.SAST).date().isoformat(),NOW.isoformat()))
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    maps=[{(r['market'],r['selection'],r['line']):r for r in odds.model_markets(run['models'][m])} for m in ('poisson','dixon_coles')]
    key=max((k for k in maps[0] if k[0] in ('1x2','goals','btts','double_chance')),key=lambda k:min(m[k]['probability'] for m in maps))
    minimum=min(m[key]['probability'] for m in maps)
    price=price_factor/minimum
    output=io.StringIO();writer=csv.writer(output);writer.writerow(odds.HEADER)
    writer.writerow([1,'Synthetic Book',*key,price,(NOW-timedelta(minutes=5)).isoformat(),odds.SETTLEMENT])
    assert client.post('/api/odds/import',json={'csv':output.getvalue(),'source':'Synthetic odds evidence'}).status_code==200
    return run,key,price


def value_report(client,**options):
    return client.get('/api/shortlist',params={'mode':'value','window':'today','max_odds_age_minutes':180,**options}).json()


def test_actual_priced_top_selection_and_home_count(client,monkeypatch):
    run,key,price=value_setup(client,monkeypatch)
    report=value_report(client)
    assert report['selection_count']==1
    selected=report['selections'][0]
    assert selected['decimal_odds']==pytest.approx(price)
    assert selected['lower_model_ev']==pytest.approx(.10)
    assert selected['bookmaker']=='Synthetic Book'
    assert selected['selection_stage']=='value_screened' and not selected['qualified']
    assert report['qualified_count']==0  # No unearned independent validation claim.
    with main.db() as c:c.execute('INSERT INTO top_five_runs(created_at,result) VALUES (?,?)',(NOW.isoformat(),json.dumps(report)))
    assert client.get('/api/dashboard').json()['top_five']['selection_count']==1


def test_negative_ev_excluded_but_probability_mode_separate(client,monkeypatch):
    value_setup(client,monkeypatch,.98)
    assert value_report(client)['selection_count']==0
    assert client.get('/api/shortlist?window=today&mode=research').json()['selection_count']==1


def test_wrong_date_old_source_stale_quote_or_model_not_promoted(client,monkeypatch):
    value_setup(client,monkeypatch)
    for sql in ["UPDATE fixtures SET status='postponed'", "UPDATE fixtures SET kickoff_utc='2026-11-03T15:00:00+00:00'",
                "UPDATE api_football_fixtures SET last_received_at='2026-10-30T12:00:00+00:00'",
                "UPDATE odds_observations SET observed_at='2026-11-02T05:00:00+00:00'"]:
        with main.db() as c:
            c.execute('SAVEPOINT test_state');c.execute(sql)
            # Independent connection cannot see an uncommitted transaction; evaluate directly.
            report=main.shortlist.build(c,main.get_settings(),NOW,'poisson','value','today',180)
            assert report['selection_count']==0
            c.execute('ROLLBACK TO test_state')


def test_utc_query_does_not_drop_matches_starting_within_two_hours(client,monkeypatch):
    value_setup(client,monkeypatch)
    current=NOW+timedelta(hours=1,minutes=1)
    monkeypatch.setattr(main,'now',lambda:current)
    report=value_report(client)
    assert report['selection_count']==1


def test_model_disagreement_and_missing_comparison_excluded(client,monkeypatch):
    run,key,price=value_setup(client,monkeypatch)
    with main.db() as c:
        run['models'].pop('dixon_coles')
        c.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps(run),run['id']))
    assert value_report(client)['selection_count']==0


def test_future_quote_not_fresh_even_with_sast_cutoff(client,monkeypatch):
    value_setup(client,monkeypatch)
    with main.db() as c:c.execute('UPDATE odds_observations SET observed_at=?',((NOW+timedelta(minutes=1)).isoformat(),))
    assert value_report(client)['selection_count']==0


def test_paper_archive_pending_then_settles_without_retroactive_bets(client,monkeypatch):
    run,key,price=value_setup(client,monkeypatch)
    report=value_report(client)
    with main.db() as c:
        for _ in range(2):c.execute('INSERT INTO top_five_runs(created_at,result) VALUES (?,?)',(NOW.isoformat(),json.dumps(report)))
    p=client.get('/api/top-five/performance').json()
    assert p['pending']==1 and p['roi'] is None
    monkeypatch.setattr(main,'now',lambda:NOW+timedelta(hours=6))
    with main.db() as c:
        f=dict(c.execute('SELECT * FROM fixtures WHERE id=1').fetchone())
        provider.record_history(c,f,{**{k:None for k in history.METRICS},'goals_home':0,'goals_away':0},main.now())
    p=client.get('/api/top-five/performance').json()
    assert p['settled']==1 and p['selections']==1 and p['roi'] is not None
    # A changed schedule is not silently settled using a different fixture identity.
    with main.db() as c:c.execute("UPDATE fixtures SET identity='rescheduled'")
    assert client.get('/api/top-five/performance').json()['settled']==0


def test_top_build_rejects_future_window_and_missing_key(client):
    assert client.post('/api/top-five/build',json={'window':'7d'}).status_code==422
    assert client.post('/api/top-five/build',json={}).status_code==409


def test_build_all_daily_leagues_archives_and_reports_partial_coverage(client,monkeypatch):
    value_setup(client,monkeypatch);save(client)
    monkeypatch.setattr(provider,'status',lambda *a:{'ok':True,'message':'Synthetic account success'})
    monkeypatch.setattr(provider,'daily_leagues',lambda *a:[{'league_id':999,'season':2026,'fixture_ids':[100]}, {'league_id':555,'season':2026,'fixture_ids':[200]}])
    monkeypatch.setattr(provider,'available_seasons',lambda *a:[2026])
    def workflow(options,*args):
        if options.league_id==555:raise main.HTTPException(502,'Synthetic unavailable coverage')
        return {'models_generated':1,'competition':'Synthetic League','seasons':[2026]}
    monkeypatch.setattr(main,'run_api_football_workflow',workflow)
    monkeypatch.setattr(provider_odds,'pages',lambda *a,**kw:[])
    class ImmediateThread:
        def __init__(self,target,args,daemon):self.target=target;self.args=args
        def start(self):self.target(*self.args)
    monkeypatch.setattr(main.threading,'Thread',ImmediateThread)
    response=client.post('/api/top-five/build',json={})
    assert response.status_code==200
    job=client.get('/api/top-five/jobs/latest').json()
    assert job['status']=='completed' and job['completed_leagues']==2
    assert job['leagues'][1]['error']=='Synthetic unavailable coverage'
    assert job['report']['window']=='today' and job['report']['selection_count']==1
    report=client.get('/api/top-five/runs/'+str(job['report_id'])).json()
    assert report['selections']==job['report']['selections']
    assert not main.WORKFLOW_LOCK.locked()


def test_daily_discovery_removal_hides_cached_fixture(client,monkeypatch):
    value_setup(client,monkeypatch)
    assert value_report(client)['selection_count']==1
    with main.db() as c:c.execute('DELETE FROM top_five_daily_fixtures')
    report=value_report(client)
    assert report['selection_count']==0
    assert 'latest daily discovery' in report['excluded'][0]['reason']


def test_conflicting_same_time_prices_and_stale_model_excluded(client,monkeypatch):
    value_setup(client,monkeypatch)
    with main.db() as c:
        original=dict(c.execute('SELECT * FROM odds_observations').fetchone())
        original['decimal_odds']+=.2;original['digest']='synthetic-conflict'
        odds.ingest(c,[original])
    assert value_report(client)['selection_count']==0
    with main.db() as c:
        c.execute("DELETE FROM odds_observations WHERE digest='synthetic-conflict'")
        row=c.execute('SELECT id,result FROM model_runs').fetchone()
        snapshot=json.loads(row['result']);snapshot['generated_at']=(NOW-timedelta(hours=25)).isoformat()
        c.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps(snapshot),row['id']))
    assert value_report(client)['selection_count']==0


def test_value_five_cap_league_caps_and_ev_order(client,monkeypatch):
    run,key,price=value_setup(client,monkeypatch)
    with main.db() as c:
        original=dict(c.execute('SELECT * FROM fixtures WHERE id=1').fetchone())
        quote=dict(c.execute('SELECT * FROM odds_observations').fetchone())
        for i in range(1,7):
            competition='Synthetic League' if i<3 else f'Synthetic League {i}'
            f={**original,'home':f'Synthetic extra {i}','competition':competition}
            f['identity']=odds.fixture_identity(f)
            cursor=c.execute('INSERT INTO fixtures(home,away,competition,kickoff_utc,status,identity,source,imported_at) VALUES (?,?,?,?,?,?,?,?)',
                tuple(f[k] for k in ('home','away','competition','kickoff_utc','status','identity','source','imported_at')))
            fid=cursor.lastrowid
            c.execute('INSERT INTO api_football_fixtures VALUES (?,?,?,?,?,?,?)',(100+i,fid,1,2,999 if i<3 else 999+i,2026,NOW.isoformat()))
            c.execute('INSERT INTO top_five_daily_fixtures VALUES (?,?,?)',(100+i,'2026-11-02',NOW.isoformat()))
            snapshot={**run,'fixture':{**run['fixture'],'id':fid,'home':f['home'],'competition':competition}}
            c.execute('INSERT INTO model_runs(fixture_id,generated_at,status,result) VALUES (?,?,?,?)',(fid,NOW.isoformat(),'experimental',json.dumps(snapshot)))
            q={**quote,'fixture_id':fid,'fixture_identity':f['identity'],'decimal_odds':quote['decimal_odds']+i*.1,'digest':f'synthetic-{i}'}
            odds.ingest(c,[q])
    report=value_report(client)
    assert report['selection_count']==5
    assert sum(c['competition']=='Synthetic League' for c in report['selections'])<=2
    lower=[c['lower_model_ev'] for c in report['selections']]
    assert lower==sorted(lower,reverse=True)


def test_pagination_filters_requested_sast_day():
    def handler(request):
        assert request.url.params['date']=='2026-11-02'
        assert request.url.params['timezone']=='Africa/Johannesburg'
        return httpx.Response(200,json=envelope('odds',[]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert provider_odds.pages(client,KEY,999,2026,day='2026-11-02')==[]


def test_midnight_during_build_cannot_archive_yesterdays_list(client,monkeypatch):
    value_setup(client,monkeypatch);save(client)
    monkeypatch.setattr(provider,'status',lambda *a:{'ok':True,'message':'Synthetic account success'})
    def discovery(*args):
        monkeypatch.setattr(main,'now',lambda:NOW+timedelta(days=1))
        return [{'league_id':999,'season':2026,'fixture_ids':[100]}]
    monkeypatch.setattr(provider,'daily_leagues',discovery)
    class ImmediateThread:
        def __init__(self,target,args,daemon):self.target=target;self.args=args
        def start(self):self.target(*self.args)
    monkeypatch.setattr(main.threading,'Thread',ImmediateThread)
    assert client.post('/api/top-five/build',json={}).status_code==200
    job=client.get('/api/top-five/jobs/latest').json()
    assert job['status']=='interrupted' and 'date changed' in job['message']
    with main.db() as c:assert c.execute('SELECT COUNT(*) FROM top_five_runs').fetchone()[0]==0
    assert not main.WORKFLOW_LOCK.locked()
