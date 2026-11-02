"""Labelled synthetic modelling data. No genuine football or profit evidence."""
import csv
import io
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from backend import history, main, models
from tests.test_foundation import client

CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)
OPTIONS = {'half_life_days':180., 'ridge':1., 'training_days':730, 'min_team_matches':5, 'elo_k':20.}


def synthetic_rows(cutoff=CUTOFF):
    rng = np.random.default_rng(72)
    attack = [-.3, .2, .1, 0.]
    defence = [-.1, .1, .05, -.05]
    rows = []
    for repeat in range(10):
        for home in range(4):
            for away in range(4):
                if home == away:
                    continue
                h = 1.5*math.exp(attack[home]+defence[away])
                a = math.exp(attack[away]+defence[home])
                metrics = {name:None for name in history.METRICS}
                metrics.update(goals_home=int(rng.poisson(h)),goals_away=int(rng.poisson(a)),xg_home=h,xg_away=a)
                rows.append({'id':len(rows)+1, 'home':f'Synthetic {home}', 'away':f'Synthetic {away}',
                             'home_key':f'synthetic {home}', 'away_key':f'synthetic {away}',
                             'competition':'Synthetic League', 'competition_key':'synthetic league',
                             'kickoff_utc':(cutoff-timedelta(days=180-len(rows))).isoformat(),
                             'metrics':metrics, 'conflicts':[]})
    return rows


def test_poisson_against_analytical_probabilities():
    h,a = 1.4,.9
    result = models.markets(h,a)
    assert result['btts']['yes'] == pytest.approx((1-math.exp(-h))*(1-math.exp(-a)),abs=1e-10)
    assert result['goals']['under_1.5'] == pytest.approx(math.exp(-h-a)*(1+h+a),abs=1e-10)
    assert sum(result['result'].values()) == pytest.approx(1)
    assert result['team_goals']['home_over_0.5'] == pytest.approx(1-math.exp(-h),abs=1e-10)
    assert result['omitted_tail'] < 1e-9


@pytest.mark.parametrize('h,a', [(0,0),(0,2),(20,20),(.001,.001),(3,1)])
def test_distribution_mass_and_complements(h,a):
    result=models.markets(h,a)
    assert sum(result['result'].values()) == pytest.approx(1)
    assert sum(result['btts'].values()) == pytest.approx(1)
    assert result['goals']['over_2.5'] + result['goals']['under_2.5'] == pytest.approx(1)
    assert result['draw_no_bet']['home']['push'] == result['result']['draw']


def test_dixon_coles_marginals_and_low_score_changes():
    baseline,_=models.score_distribution(1.4,.9)
    adjusted,_=models.score_distribution(1.4,.9,-.1)
    assert adjusted[0,0] > baseline[0,0]
    assert adjusted[1,1] > baseline[1,1]
    assert adjusted[0,1] < baseline[0,1]
    assert adjusted.sum(axis=0) == pytest.approx(baseline.sum(axis=0),abs=1e-12)
    assert adjusted.sum(axis=1) == pytest.approx(baseline.sum(axis=1),abs=1e-12)


@pytest.mark.parametrize('h,a,rho', [(-1,1,0),(1,float('nan'),0),(1,1,2),(30,1,0),(2,2,.5)])
def test_invalid_distribution_rejected(h,a,rho):
    with pytest.raises(models.ModelUnavailable):
        models.markets(h,a,rho)


def test_xg_fit_recovers_known_opponent_and_venue_effects():
    rows=synthetic_rows()
    fit=models.fit_strength(rows,CUTOFF,ridge=.01,metric='xg')
    home,away=fit.rates('synthetic 0','synthetic 1')
    assert home == pytest.approx(1.5*math.exp(-.3+.1),abs=.005)
    assert away == pytest.approx(math.exp(.2-.1),abs=.005)
    assert fit.diagnostics()['home_goal_multiplier'] == pytest.approx(1.5,abs=.005)
    assert fit.rates('synthetic 2','synthetic 1')[0] > fit.rates('synthetic 2','synthetic 0')[0]


def test_fitted_models_and_evidence_limits():
    rows=synthetic_rows()
    fixture={'home':'Synthetic 0','away':'Synthetic 1'}
    result=models.analyse(rows,fixture,CUTOFF,OPTIONS,30)
    assert result['status'] == 'experimental'
    assert {'poisson','dixon_coles','xg_poisson'} == set(result['models'])
    for model in result['models'].values():
        assert sum(model['result'].values()) == pytest.approx(1)
        assert not model['diagnostics']['out_of_sample_validated']
    assert result['calibration'] is None and result['uncertainty'] is None
    assert result['ensemble'] is None and result['recommendation'] is None
    assert result['form']['home']['reported_xg_sample'] == 20
    assert result['form']['home']['effective_sample'] <= 20


def test_missing_xg_never_derived_from_goals():
    rows=synthetic_rows()
    for row in rows:
        row['metrics']['xg_home']=None
    result=models.analyse(rows,{'home':'Synthetic 0','away':'Synthetic 1'},CUTOFF,OPTIONS,30)
    assert 'poisson' in result['models']
    assert 'xg_poisson' not in result['models']
    assert 'found 0' in result['unavailable']['xg_poisson']


def test_insufficient_sample_no_uniform_fallback():
    result=models.analyse(synthetic_rows()[:3],{'home':'Synthetic 0','away':'Synthetic 1'},CUTOFF,OPTIONS,1)
    assert result['models'] == {}
    assert result['status'] == 'insufficient_data'
    assert '30' in result['unavailable']['poisson']


def test_unknown_team_and_disconnected_graph_rejected():
    rows=synthetic_rows()
    with pytest.raises(models.ModelUnavailable,match='appearances'):
        models.check_coverage(rows,'unknown','synthetic 1',30,5)
    disconnected=[{**row,'home_key':'other home','away_key':'other away'} for row in rows]
    with pytest.raises(models.ModelUnavailable,match='disconnected'):
        models.check_coverage(rows+disconnected,'synthetic 0','synthetic 1',30,5)


def test_elo_conserves_total_and_draw_is_not_win_probability():
    home,away=models.elo_update(1500,1500,2,0)
    assert (home,away) == (1510,1490)
    assert home+away == 3000
    assert models.elo_update(1500,1500,1,1) == (1500,1500)
    assert models.elo_update(1500,1500,0,2) == (1490,1510)


def test_recency_weights_favour_recent_matches():
    rows=synthetic_rows()
    weights=models.recency_weights(rows,CUTOFF,180)
    assert weights[-1] > weights[0]
    assert weights[0] == pytest.approx(.5)


def fixture_and_history(client):
    current=main.now()
    rows=synthetic_rows(current)
    output=io.StringIO()
    writer=csv.writer(output)
    writer.writerow(history.REQUIRED+['xg_home','xg_away'])
    for row in rows:
        kickoff=datetime.fromisoformat(row['kickoff_utc'])
        m=row['metrics']
        writer.writerow([row['home'],row['away'],row['competition'],kickoff.astimezone(main.SAST).isoformat(),m['goals_home'],m['goals_away'],(kickoff+timedelta(hours=3)).isoformat(),m['xg_home'],m['xg_away']])
    assert client.post('/api/history/import',json={'csv':output.getvalue(),'source':'Synthetic model test source'}).status_code == 200
    csv_fixture='home,away,competition,kickoff_sast,status\nSynthetic 0,Synthetic 1,Synthetic League,'+(current+timedelta(days=1)).astimezone(main.SAST).isoformat()+',scheduled'
    assert client.post('/api/fixtures/import',json={'csv':csv_fixture}).status_code == 200
    return rows,csv_fixture


def test_analysis_snapshot_immutable_and_not_top_five(client):
    fixture_and_history(client)
    response=client.post('/api/fixtures/1/analysis',json={})
    assert response.status_code == 200
    first=response.json()
    assert first['status'] == 'experimental'
    assert first['dataset_fingerprint']
    assert len(first['observation_ids']) == 120
    assert client.get('/api/analysis/'+str(first['id'])).json() == first
    second=client.post('/api/fixtures/1/analysis',json={'half_life_days':90}).json()
    assert second['id'] != first['id']
    assert client.get('/api/analysis/'+str(first['id'])).json() == first
    assert len(client.get('/api/fixtures/1/analyses').json()) == 2
    assert client.get('/api/dashboard').json()['selections'] == []


def test_analysis_rejects_ineligible_fixture(client):
    _,csv_fixture=fixture_and_history(client)
    client.post('/api/fixtures/import',json={'csv':csv_fixture.replace('scheduled','live')})
    assert client.post('/api/fixtures/1/analysis',json={}).status_code == 409
    assert client.get('/api/fixtures/1/analyses').json() == []


def test_receipt_time_prevents_training_leakage(client):
    fixture_and_history(client)
    with main.db() as connection:
        rows,_,_=models.load_dataset(connection,'Synthetic League',main.now()-timedelta(days=1),730)
    assert rows == []


def test_analysis_does_not_cross_competitions(client):
    fixture_and_history(client)
    with main.db() as connection:
        rows,_,_=models.load_dataset(connection,'Different League',main.now(),730)
    assert rows == []


def test_analysis_options_and_unknown_ids(client):
    assert client.post('/api/fixtures/999/analysis',json={}).status_code == 404
    assert client.get('/api/analysis/999').status_code == 404
    assert client.post('/api/fixtures/1/analysis',json={'ridge':0}).status_code == 422


def test_probability_filter_does_not_manipulate_estimates(client):
    fixture_and_history(client)
    first=client.post('/api/fixtures/1/analysis',json={}).json()
    settings=client.get('/api/settings').json()
    settings['min_probability']=.99
    client.put('/api/settings',json=settings)
    second=client.post('/api/fixtures/1/analysis',json={}).json()
    assert first['dataset_fingerprint'] == second['dataset_fingerprint']
    for model in first['models']:
        assert first['models'][model]['result'] == pytest.approx(second['models'][model]['result'],abs=1e-7)


def test_effective_sample_guard_rejects_too_short_recency_window():
    rows=synthetic_rows()
    result=models.analyse(rows,{'home':'Synthetic 0','away':'Synthetic 1'},CUTOFF,{**OPTIONS,'half_life_days':7},30)
    assert result['models'] == {}
    assert 'effective sample' in result['unavailable']['poisson']


def test_elo_does_not_compare_disconnected_groups():
    rows=synthetic_rows()
    other=[{**r,'home_key':'unrelated home','away_key':'unrelated away'} for r in rows]
    assert models.elo_context(rows+other,'synthetic 0','unrelated home') is None
