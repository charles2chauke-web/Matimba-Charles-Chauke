"""Synthetic daily-review cases; never genuine qualified betting selections."""
import json
from datetime import timedelta
import pytest
from backend import main,shortlist
from tests.test_foundation import client
from tests.test_models import fixture_and_history


def prepare(client,monkeypatch):
    rows,csv=fixture_and_history(client)
    first=client.get('/api/fixtures').json()[0]
    kickoff=main.datetime.fromisoformat(first['kickoff_utc'])
    # The synthetic fixture is tomorrow; use a 24-hour window after moving its
    # analysis clock slightly forward, without changing any receipt timestamps.
    current=main.now()+timedelta(minutes=2)
    monkeypatch.setattr(main,'now',lambda:current)
    settings=client.get('/api/settings').json();settings['window']='today'
    client.put('/api/settings',json=settings)
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    return run,settings,current


def test_empty_page_has_actual_explanation_not_placeholder(client):
    result=client.get('/api/research/shortlist').json()
    assert result['research_watchlist']==[] and result['qualified']==[]
    assert result['qualification_available'] is False
    assert 'Production qualification' in result['qualification_explanation']


def test_research_candidate_real_math_no_fake_odds_or_qualification(client,monkeypatch):
    run,settings,current=prepare(client,monkeypatch)
    report=client.get('/api/research/shortlist').json()
    assert len(report['research_watchlist'])==1
    item=report['research_watchlist'][0]
    assert item['probability']>=settings['min_probability']
    assert item['fair_odds']==pytest.approx(1/item['probability'])
    assert item['decimal_odds'] is None and item['expected_value'] is None
    assert not item['qualified'] and report['qualified_count']==0
    assert any('calibration' in reason for reason in item['qualification_blockers'])
    assert any('odds' in reason for reason in item['qualification_blockers'])
    assert len({c['fixture_id'] for c in report['research_watchlist']})==len(report['research_watchlist'])


def test_probability_filter_does_not_raise_estimates(client,monkeypatch):
    run,settings,current=prepare(client,monkeypatch)
    first=client.get('/api/research/shortlist').json()['research_watchlist'][0]['probability']
    settings['min_probability']=1
    client.put('/api/settings',json=settings)
    report=client.get('/api/research/shortlist').json()
    assert not report['research_watchlist']
    stored=client.get('/api/analysis/'+str(run['id'])).json()
    assert stored==run
    assert 'not adjusted' in report['excluded'][0]['reason']


def test_stale_model_and_unavailable_model_are_explained(client,monkeypatch):
    run,settings,current=prepare(client,monkeypatch)
    # Explicit unavailable model must not silently switch to a different model.
    with main.db() as c:
        stored={k:v for k,v in run.items() if k!='id'}
        stored['models'].pop('xg_poisson',None)
        c.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps(stored),run['id']))
    assert not client.get('/api/research/shortlist?model=xg_poisson').json()['research_watchlist']
    with main.db() as c:
        stored['generated_at']=(current-timedelta(hours=25)).isoformat()
        c.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps(stored),run['id']))
    report=client.get('/api/research/shortlist').json()
    assert 'freshness' in report['excluded'][0]['reason']


def test_sast_today_window_and_non_scheduled_statuses(client,monkeypatch):
    run,settings,current=prepare(client,monkeypatch)
    with main.db() as c:
        c.execute("UPDATE fixtures SET status='cancelled'")
    assert client.get('/api/research/shortlist').json()['fixtures_in_window']==0
    settings['window']='today'
    client.put('/api/settings',json=settings)
    with main.db() as c:c.execute("UPDATE fixtures SET status='scheduled',kickoff_utc=?",((current+timedelta(days=1)).isoformat(),))
    assert client.get('/api/research/shortlist').json()['fixtures_in_window']==0


def test_league_limit_and_five_cap_are_enforced(client,monkeypatch):
    run,settings,current=prepare(client,monkeypatch)
    with main.db() as c:
        original=dict(c.execute('SELECT * FROM fixtures WHERE id=1').fetchone())
        for i in range(6):
            home=f'Synthetic Extra {i}'
            competition='Synthetic League' if i<3 else f'Synthetic Other {i}'
            identity=json.dumps([home,'Away',competition,original['kickoff_utc']])
            cursor=c.execute('''INSERT INTO fixtures(home,away,competition,kickoff_utc,status,identity,source,imported_at)
                VALUES (?,?,?,?,?,?,?,?)''',(home,original['away'],competition,original['kickoff_utc'],'scheduled',identity,'Synthetic',current.isoformat()))
            copy={k:v for k,v in run.items() if k!='id'}
            copy['fixture']={**copy['fixture'],'id':cursor.lastrowid,'home':home,'competition':competition}
            c.execute('INSERT INTO model_runs(fixture_id,generated_at,status,result) VALUES (?,?,?,?)',(cursor.lastrowid,current.isoformat(),'experimental',json.dumps(copy)))
    report=client.get('/api/research/shortlist').json()
    watch=report['research_watchlist']
    assert len(watch)==5
    assert sum(c['competition']=='Synthetic League' for c in watch)<=settings['max_per_league']
    assert report['qualified']==[]


def test_unknown_value_and_negative_ev_do_not_get_called_positive_value():
    missing={'probability':.95,'expected_value':None}
    negative={'probability':.80,'expected_value':-.1}
    positive={'probability':.80,'expected_value':.1}
    assert shortlist.research_order(missing,.03)>shortlist.research_order(negative,.03)
    assert shortlist.research_order(positive,.03)>shortlist.research_order(missing,.03)
