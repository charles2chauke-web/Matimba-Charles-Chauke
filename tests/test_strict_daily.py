"""Strict today/market/count-data contracts on labelled synthetic fixtures."""
import copy,json
from datetime import timedelta
import httpx
import pytest
from backend import main,daily_policy,strict_shortlist,provider_statistics,count_models,api_football as provider,history,provider_odds,odds
from tests.test_foundation import client
from tests.test_api_football import NOW,KEY,fixture,envelope,reset_rate_state
from tests.test_models import synthetic_rows,OPTIONS,fixture_and_history
from tests.test_top_five import value_setup


def test_requested_market_contract_exact():
    assert daily_policy.MARKETS=={('goals','over','1.5'),('goals','over','2.5'),('1x2','home',''),('1x2','away',''),('corners','over','7.5'),('yellow_cards','over','2.5')}
    assert not daily_policy.is_today((NOW+timedelta(days=1)).isoformat(),NOW)
    assert not daily_policy.is_today((NOW-timedelta(seconds=1)).isoformat(),NOW)
    # At 22:30 UTC the SAST generation date has already changed.
    at=NOW.replace(hour=22,minute=30)
    assert daily_policy.is_today((at+timedelta(hours=1)).isoformat(),at)
    assert not daily_policy.is_today((at+timedelta(days=1)).isoformat(),at)


def test_no_future_analysis_via_direct_api(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    fixture_and_history(client)
    with main.db() as c:c.execute('UPDATE fixtures SET kickoff_utc=?',((NOW+timedelta(days=1)).isoformat(),))
    assert client.post('/api/fixtures/1/analysis',json={}).status_code==409
    assert not client.get('/api/fixtures').json()[0]['research_eligible']


def strict_setup(client,monkeypatch):
    run,_,_=value_setup(client,monkeypatch)
    # Deliberately make over 0.5 the largest estimate. It must NEVER win selection.
    for name in ('poisson','dixon_coles'):
        run['models'][name]['goals'].update({'over_0.5':.999,'over_1.5':.82,'over_2.5':.76})
        run['models'][name]['result'].update({'home':.7,'draw':.2,'away':.1})
    with main.db() as c:c.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps(run),run['id']))
    return run


def test_strict_ui_endpoint_cannot_choose_over_half_draw_or_double_chance(client,monkeypatch):
    strict_setup(client,monkeypatch)
    r=client.get('/api/daily-predictions').json()
    assert r['selection_count']==1
    assert r['selections'][0]['market']=='goals' and r['selections'][0]['line']=='1.5'
    assert r['selections'][0]['probability']==pytest.approx(.82)
    assert client.get('/api/daily-predictions?min_probability=0.5').status_code==422


def test_missing_comparison_and_yesterday_predictions_withheld(client,monkeypatch):
    run=strict_setup(client,monkeypatch)
    run['models'].pop('dixon_coles')
    with main.db() as c:c.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps(run),run['id']))
    assert client.get('/api/daily-predictions').json()['selection_count']==0
    run['generated_at']=(NOW-timedelta(days=1)).isoformat()
    with main.db() as c:c.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps(run),run['id']))
    assert 'not generated today' in client.get('/api/daily-predictions').json()['excluded'][0]['reason']


def stat_payload():
    return [{'team':{'id':1},'statistics':[{'type':'Corner Kicks','value':0},{'type':'Yellow Cards','value':2},{'type':'Red Cards','value':None}]},
            {'team':{'id':2},'statistics':[{'type':'Corner Kicks','value':5},{'type':'Yellow Cards','value':None}]}]


def test_stat_id_count_and_missing_contract():
    d=provider_statistics.parse(stat_payload(),1,2)
    assert d['corners_home']==0 and d['corners_away']==5
    assert d['yellow_away'] is None and d['red_home'] is None
    for change in ('team','negative','duplicate'):
        p=stat_payload()
        if change=='team':p[0]['team']['id']=999
        elif change=='negative':p[0]['statistics'][0]['value']=-1
        else:p[0]['statistics'].append({'type':'Corner Kicks','value':3})
        with pytest.raises(provider.ProviderError):provider_statistics.parse(p,1,2)
    assert provider_statistics.parse([],1,2) is None


def test_count_models_use_reported_counts_not_goals():
    rows=synthetic_rows(NOW)
    for i,r in enumerate(rows):
        r['metrics'].update({'corners_home':4+i%4,'corners_away':3+i%3,'yellow_home':1+i%3,'yellow_away':1+i%2})
    fixture={'home':'Synthetic 0','away':'Synthetic 1'}
    result,missing=count_models.analyse(rows,fixture,NOW,OPTIONS,30)
    assert not missing and set(result)=={'corners','yellow_cards'}
    assert result['corners']['markets'][0]['line']=='7.5'
    assert result['yellow_cards']['markets'][0]['line']=='2.5'
    for c in result.values():
        p=c['markets'][0]['probability'];assert 0<p<1
    for r in rows:r['metrics']['yellow_home']=None
    result,missing=count_models.analyse(rows,fixture,NOW,OPTIONS,30)
    assert 'yellow_cards' not in result and 'yellow_cards' in missing


def test_statistics_receipt_cache_and_goal_refresh_preserves_counts(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    dataset={'league_id':39,'season':2026,'name':'Synthetic','country':'Test','fixtures':[provider.parse_fixture(fixture(1,'FT',NOW-timedelta(days=1)),39,2026,NOW)]}
    with main.db() as c:provider.ingest(c,[dataset],NOW)
    calls=[]
    def handler(request):
        calls.append(request);return httpx.Response(200,json=envelope('fixtures/statistics',stat_payload()))
    with httpx.Client(transport=httpx.MockTransport(handler)) as api:
        result=provider_statistics.enrich(main.db,api,KEY,39,NOW,10,clock=lambda:NOW+timedelta(seconds=2))
        assert result['calls']==1
        result=provider_statistics.enrich(main.db,api,KEY,39,NOW+timedelta(minutes=1),10,clock=lambda:NOW+timedelta(minutes=1))
        assert result['calls']==0 and result['cached']==1
    assert len(calls)==1
    with main.db() as c:
        match=c.execute('SELECT id FROM historical_matches').fetchone()[0]
        prior=history.resolve(history.observations(c,match,NOW.isoformat()))['metrics']
        assert prior['corners_home'] is None
        provider.ingest(c,[dataset],NOW+timedelta(minutes=2))
        latest=history.resolve(history.observations(c,match))['metrics']
        assert latest['corners_home']==0 and latest['corners_away']==5


def test_count_odds_catalog_does_not_infer_generic_card_points(monkeypatch):
    monkeypatch.setattr(provider_odds,'EXTRA_MARKETS',{})
    rows=[{'id':444,'name':'Total Corners'},{'id':445,'name':'Total Yellow Cards'},{'id':446,'name':'Cards Over/Under'}]
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=envelope('odds/bets',rows)))) as api:
        coverage=provider_odds.load_count_markets(api,KEY)
    assert coverage=={'corners':True,'yellow_cards':True}
    assert provider_odds.market({'id':444,'name':'Total Corners','values':[{'value':'Over 7.5','odd':'1.8'}]})==[('corners','over','7.5',1.8)]
    assert provider_odds.market({'id':445,'name':'Total Yellow Cards','values':[{'value':'Over 2.5','odd':'1.7'}]})==[('yellow_cards','over','2.5',1.7)]
    assert not provider_odds.market({'id':446,'name':'Cards Over/Under','values':[{'value':'Over 2.5','odd':'1.7'}]})


def test_major_competitions_priority_and_one_outcome_per_match(client,monkeypatch):
    run=strict_setup(client,monkeypatch)
    with main.db() as c:
        f=dict(c.execute('SELECT * FROM fixtures WHERE id=1').fetchone())
        for i,lid in enumerate([39,140,135,78,61],2):
            name=f'Synthetic Major {lid}'
            copy_fixture={**f,'home':f'Major home {i}','competition':name}
            identity=odds.fixture_identity(copy_fixture)
            cursor=c.execute('INSERT INTO fixtures(home,away,competition,kickoff_utc,status,identity,source,imported_at) VALUES (?,?,?,?,?,?,?,?)',
                (copy_fixture['home'],f['away'],name,f['kickoff_utc'],'scheduled',identity,provider.SOURCE,NOW.isoformat()))
            fid=cursor.lastrowid
            c.execute('INSERT INTO api_football_fixtures VALUES (?,?,?,?,?,?,?)',(100+i,fid,1,2,lid,2026,NOW.isoformat()))
            c.execute('INSERT INTO top_five_daily_fixtures VALUES (?,?,?)',(100+i,'2026-11-02',NOW.isoformat()))
            snapshot={**run,'fixture':{**run['fixture'],'id':fid,'home':copy_fixture['home'],'competition':name}}
            c.execute('INSERT INTO model_runs(fixture_id,generated_at,status,result) VALUES (?,?,?,?)',(fid,NOW.isoformat(),'experimental',json.dumps(snapshot)))
    report=client.get('/api/daily-predictions').json()
    assert report['selection_count']==5 and all(s['major_competition'] for s in report['selections'])
    assert len({s['fixture_id'] for s in report['selections']})==5
    assert len({s['league_id'] for s in report['selections']})==5
    assert all((s['market'],s['selection'],s['line']) in daily_policy.MARKETS for s in report['selections'])


def test_count_selection_uses_its_own_sample_and_withholds_missing_prices(client,monkeypatch):
    run=strict_setup(client,monkeypatch)
    run['count_models']={'corners':{'method':'Synthetic count fit','diagnostics':{'sample':100,'effective_sample':80},
        'markets':[{'market':'corners','selection':'over','line':'7.5','probability':.9,'comparison_probability':.88,'push':0,'settlement':'regulation_90'}]}}
    with main.db() as c:c.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps(run),run['id']))
    report=client.get('/api/daily-predictions').json()
    assert report['selections'][0]['market']=='corners' and report['selections'][0]['probability']==.88
    assert report['selections'][0]['decimal_odds'] is None
    assert client.get('/api/daily-predictions?mode=value').json()['selection_count']==0
    run['count_models']['corners']['diagnostics']['sample']=4
    with main.db() as c:c.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps(run),run['id']))
    assert client.get('/api/daily-predictions').json()['selections'][0]['market']=='goals'


def test_workflow_future_horizon_rejected(client):
    assert client.post('/api/workflow/run',json={'horizon_days':7}).status_code==422


def test_legacy_top_url_also_enforces_strict_markets(client,monkeypatch):
    strict_setup(client,monkeypatch)
    report=client.get('/api/shortlist?window=7d').json()
    assert report['window']=='today'
    assert report['selections'][0]['line']=='1.5'


def test_corner_paper_result_never_settles_from_goals(client,monkeypatch):
    run=strict_setup(client,monkeypatch)
    run['count_models']={'corners':{'method':'Synthetic count fit','diagnostics':{'sample':100,'effective_sample':80},
        'markets':[{'market':'corners','selection':'over','line':'7.5','probability':.9,'comparison_probability':.88,'push':0,'settlement':'regulation_90'}]}}
    with main.db() as c:
        c.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps(run),run['id']))
        f=dict(c.execute('SELECT * FROM fixtures WHERE id=1').fetchone())
    csv='fixture_id,bookmaker,market,selection,line,decimal_odds,observed_at,settlement\n1,Synthetic corner book,corners,over,7.5,1.5,'+NOW.isoformat()+',regulation_90'
    assert client.post('/api/odds/import',json={'csv':csv,'source':'Synthetic corner quote'}).status_code==200
    report=client.get('/api/daily-predictions?mode=value').json()
    assert report['selections'][0]['market']=='corners'
    with main.db() as c:c.execute('INSERT INTO top_five_runs(created_at,result) VALUES (?,?)',(report['generated_at'],json.dumps(report)))
    later=NOW+timedelta(hours=6);monkeypatch.setattr(main,'now',lambda:later)
    metrics={**{k:None for k in history.METRICS},'goals_home':5,'goals_away':4}
    with main.db() as c:provider.record_history(c,f,metrics,later)
    assert client.get('/api/top-five/performance').json()['settled']==0
    later+=timedelta(minutes=1);metrics.update(corners_home=4,corners_away=3)
    with main.db() as c:provider.record_history(c,f,metrics,later)
    performance=client.get('/api/top-five/performance').json()
    assert performance['settled']==1 and performance['profit_units']==-1
