"""Synthetic time-indexed worlds only: never real betting-performance evidence."""
import csv
import io
import json
import math
from datetime import datetime,timedelta,timezone

import pytest
from backend import backtesting,history,main,models,odds
from tests.test_foundation import client
from tests.test_models import synthetic_rows

NOW=datetime(2026,11,2,12,tzinfo=timezone.utc)
OPTIONS={'competition':'Synthetic Replay League','method':'saved_predictions','calibration_start':'2026-07-01',
         'test_start':'2026-08-01','test_end':'2026-08-08','calibration':'temperature',
         'model_options':{'training_days':730,'half_life_days':180.,'ridge':1.,'min_team_matches':5,'elo_k':20.},
         'minimum_sample':30,'decision_lead_minutes':60,'strategy_model':'poisson','strategy_probabilities':'raw',
         'market':'1x2','selection':'home','line':'','min_probability':0.,'min_ev':-1.,
         'max_odds_age_minutes':60.,'max_model_age_hours':24.,'flat_stake':1.}


def seed_world(connection,warmup=False,quote_market='1x2',quote_price=2.2):
    """Receipt times and stored forecasts are explicitly synthetic test fixtures."""
    if warmup:
        for row in synthetic_rows(datetime(2026,7,1,tzinfo=timezone.utc)):
            kickoff=datetime.fromisoformat(row['kickoff_utc'])
            add_result(connection,row['home'],row['away'],kickoff,row['metrics']['goals_home'],row['metrics']['goals_away'],row['metrics'])
    dates=[datetime(2026,7,1,16,tzinfo=timezone.utc)+timedelta(days=i) for i in range(24)]
    dates += [datetime(2026,8,1,16,tzinfo=timezone.utc)+timedelta(days=i) for i in range(8)]
    ids=[]
    for index,kickoff in enumerate(dates):
        outcome=(0,1,2,0)[index%4]
        gh,ga=((2,0),(1,1),(0,2))[outcome]
        match_id=add_result(connection,'Synthetic 0','Synthetic 1',kickoff,gh,ga)
        identity=connection.execute('SELECT identity FROM historical_matches WHERE id=?',(match_id,)).fetchone()[0]
        connection.execute('''INSERT INTO fixtures(home,away,competition,kickoff_utc,status,identity,source,imported_at)
            VALUES (?,?,?,?,?,?,?,?)''',('Synthetic 0','Synthetic 1','Synthetic Replay League',kickoff.isoformat(),'completed',identity,'Synthetic test fixture',(kickoff-timedelta(days=2)).isoformat()))
        fixture=dict(connection.execute('SELECT * FROM fixtures WHERE identity=?',(identity,)).fetchone())
        connection.execute('INSERT INTO fixture_observations(fixture_id,identity,status,recorded_at,source) VALUES (?,?,?,?,?)',
            (fixture['id'],identity,'scheduled',(kickoff-timedelta(days=2)).isoformat(),'Synthetic schedule observation'))
        connection.execute('INSERT INTO fixture_observations(fixture_id,identity,status,recorded_at,source) VALUES (?,?,?,?,?)',
            (fixture['id'],identity,'completed',(kickoff+timedelta(hours=3)).isoformat(),'Synthetic result-status observation'))
        decision=kickoff-timedelta(hours=1)
        generated=decision-timedelta(minutes=10)
        model={'poisson':models.markets(2.,.7),'dixon_coles':models.markets(2.,.7,-.05),'xg_poisson':models.markets(1.8,.7)}
        snapshot={'fixture':{**{k:fixture[k] for k in ('id','home','away','competition','status','source','imported_at')},
                  'kickoff_sast':kickoff.astimezone(history.SAST).isoformat()},'generated_at':generated.isoformat(),
                  'training_cutoff':generated.isoformat(),'models':model,'unavailable':{},'model_version':'synthetic-fixture-only',
                  'dataset_fingerprint':'synthetic-pre-match-artifact','observation_ids':[]}
        snapshot['options']=OPTIONS['model_options'].copy()
        snapshot['minimum_sample']=30
        snapshot['fixture']['status']='scheduled'
        connection.execute('INSERT INTO model_runs(fixture_id,generated_at,status,result) VALUES (?,?,?,?)',
                           (fixture['id'],generated.isoformat(),'experimental',json.dumps(snapshot)))
        text=','.join(odds.HEADER)+f"\n{fixture['id']},Synthetic Book,{quote_market},home,,{quote_price},{(decision-timedelta(minutes=10)).isoformat()},regulation_90\n"
        rows=odds.parse(text,'Synthetic odds evidence','',decision-timedelta(minutes=5),{fixture['id']:fixture})
        odds.ingest(connection,rows)
        ids.append(match_id)
    return ids


def add_result(connection,home,away,kickoff,gh,ga,metrics=None):
    output=io.StringIO();writer=csv.writer(output)
    writer.writerow(history.REQUIRED+['xg_home','xg_away'])
    writer.writerow([home,away,'Synthetic Replay League',kickoff.astimezone(history.SAST).isoformat(),gh,ga,
        (kickoff+timedelta(hours=2)).isoformat(),(metrics or {}).get('xg_home',''),(metrics or {}).get('xg_away','')])
    rows=history.parse(output.getvalue(),'Synthetic historical source','',NOW)
    history.ingest(connection,rows,(kickoff+timedelta(hours=3)).isoformat())
    return connection.execute('SELECT id FROM historical_matches WHERE identity=?',(rows[0]['identity'],)).fetchone()[0]


def test_predictive_metrics_against_known_scores():
    report=backtesting.predictive_metrics([[.8,.1,.1]],[0])
    assert report['sample']==1 and report['accuracy']==1
    assert report['brier']==pytest.approx(.06)
    assert report['log_loss']==pytest.approx(-math.log(.8))
    assert sum(b['count'] for b in report['calibration']['home'])==1
    empty=backtesting.predictive_metrics([],[])
    assert empty['accuracy'] is None and empty['sample']==0


def test_temperature_learned_and_mass_preserved():
    probabilities=[[.8,.1,.1]]*24
    outcomes=[0]*12+[1]*6+[2]*6
    fit=backtesting.fit_temperature(probabilities,outcomes)
    assert fit['status']=='fitted_for_research'
    adjusted=backtesting.temperature_scale(probabilities,fit['temperature']).tolist()
    assert sum(adjusted[0])==pytest.approx(1)
    assert backtesting.predictive_metrics(adjusted,outcomes)['log_loss']<backtesting.predictive_metrics(probabilities,outcomes)['log_loss']
    assert backtesting.fit_temperature(probabilities[:3],outcomes[:3])['temperature'] is None


def test_financial_math_losses_pushes_roi_and_drawdown():
    trades=[{'match_id':i,'stake':1.,'profit':profit,'outcome':outcome,'decision_at':str(i),'settled_at':str(i)}
            for i,(profit,outcome) in enumerate([(1,'win'),(-1,'loss'),(0,'push'),(.5,'win')])]
    report=backtesting.financial_metrics(trades)
    assert report['net_profit']==.5 and report['roi']==.125
    assert report['max_drawdown_units']==1
    assert report['wins']==2 and report['losses']==1 and report['pushes']==1
    assert report['win_rate']==pytest.approx(2/3)
    assert backtesting.financial_metrics([])['roi'] is None


@pytest.mark.parametrize('market,selection,line,gh,ga,expected',[
 ('1x2','home','',2,1,'win'),('1x2','draw','',1,1,'win'),('draw_no_bet','home','',1,1,'push'),
 ('goals','over','2.5',2,1,'win'),('goals','under','2.5',2,1,'loss'),('btts','no','',2,0,'win'),
 ('team_goals_away','over','.5',1,0,'loss'),('double_chance','away_or_draw','',2,2,'win')])
def test_market_settlement(market,selection,line,gh,ga,expected):
    assert backtesting.settlement(market,selection,line,gh,ga)==expected


def test_saved_audit_is_chronological_and_uses_historical_odds(client):
    with main.db() as connection:
        seed_world(connection)
        report=backtesting.run(connection,OPTIONS.copy(),NOW)
    assert report['status']=='completed_research'
    assert report['coverage']=={'targets':32,'calibration_targets':24,'test_targets':8,'unavailable_targets':0}
    assert report['models']['poisson']['raw']['sample']==8
    assert report['models']['poisson']['calibration_fit']['sample']==24
    assert report['models']['poisson']['calibrated']['sample']==8
    assert report['strategy']['financial']['bets']==8
    assert report['strategy']['financial']['net_profit']==pytest.approx(.8)
    assert report['strategy']['financial']['roi']==pytest.approx(.1)
    assert not report['deployment_approved'] and report['recommendation'] is None
    assert all(e['forecast_at']<=e['decision_at'] for e in report['entries'])


def test_holdout_outcomes_cannot_fit_temperature(client):
    with main.db() as connection:
        ids=seed_world(connection)
        first=backtesting.run(connection,OPTIONS.copy(),NOW)
        for match_id in ids[24:]:
            row=connection.execute('SELECT metrics FROM historical_observations WHERE match_id=?',(match_id,)).fetchone()
            metrics=json.loads(row['metrics']);metrics.update(goals_home=0,goals_away=8)
            connection.execute('UPDATE historical_observations SET metrics=? WHERE match_id=?',(json.dumps(metrics),match_id))
        second=backtesting.run(connection,OPTIONS.copy(),NOW)
    for name in backtesting.NAMES:
        assert first['models'][name]['calibration_fit']==second['models'][name]['calibration_fit']
    assert first['strategy']['financial']['net_profit']!=second['strategy']['financial']['net_profit']


def test_calibration_labels_received_after_freeze_are_excluded(client):
    with main.db() as connection:
        ids=seed_world(connection)
        connection.execute('UPDATE historical_observations SET imported_at=? WHERE match_id=?',(NOW.isoformat(),ids[23]))
        report=backtesting.run(connection,OPTIONS.copy(),NOW)
    assert report['models']['poisson']['calibration_fit']['sample']==23
    assert ids[23] not in report['models']['poisson']['calibration_fit']['match_ids']


def test_odds_received_later_are_not_substituted(client):
    with main.db() as connection:
        seed_world(connection)
        connection.execute('UPDATE odds_observations SET imported_at=?',(NOW.isoformat(),))
        report=backtesting.run(connection,OPTIONS.copy(),NOW)
    assert report['models']['poisson']['raw']['sample']==8
    assert report['strategy']['financial']['bets']==0
    assert report['strategy']['financial']['net_profit'] is None
    assert report['strategy']['excluded']['no_fresh_historical_odds']==8


def test_walk_forward_respects_receipts_and_excludes_own_result(client):
    with main.db() as connection:
        seed_world(connection,warmup=True)
        report=backtesting.run(connection,{**OPTIONS,'method':'walk_forward'},NOW)
        for entry in report['entries']:
            for observation_id in entry.get('training_observation_ids',[]):
                row=connection.execute('SELECT match_id,imported_at,observed_at FROM historical_observations WHERE id=?',(observation_id,)).fetchone()
                assert row['match_id']!=entry['match_id']
                assert row['imported_at']<=entry['decision_at'] and row['observed_at']<=entry['decision_at']
    assert report['models']['poisson']['raw']['sample']==8
    assert all(e.get('source_run_id') is None for e in report['entries'])


def test_newly_imported_history_does_not_fake_a_backtest(client):
    with main.db() as connection:
        seed_world(connection,warmup=True)
        connection.execute('UPDATE historical_observations SET imported_at=?',(NOW.isoformat(),))
        report=backtesting.run(connection,{**OPTIONS,'method':'walk_forward'},NOW)
    assert report['status']=='no_evaluable_predictions'
    assert report['models']['poisson']['raw']['accuracy'] is None
    assert report['strategy']['financial']['roi'] is None


def test_saved_forecasts_after_decision_cannot_be_used(client):
    with main.db() as connection:
        seed_world(connection)
        rows=connection.execute('SELECT id,result FROM model_runs').fetchall()
        for row in rows:
            stored=json.loads(row['result'])
            late=(datetime.fromisoformat(stored['fixture']['kickoff_sast'])-timedelta(minutes=30)).astimezone(timezone.utc).isoformat()
            stored['generated_at']=late;stored['training_cutoff']=late
            connection.execute('UPDATE model_runs SET generated_at=?,result=? WHERE id=?',(late,json.dumps(stored),row['id']))
        report=backtesting.run(connection,OPTIONS.copy(),NOW)
    assert report['status']=='no_evaluable_predictions'


def test_invalid_period_and_strategy_rejected(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    for changes in ({'test_start':'2026-06-01'},{'test_end':'2099-01-01'},{'market':'corners'},
        {'strategy_probabilities':'calibrated_1x2','market':'goals','selection':'over','line':'2.5'}):
        assert client.post('/api/backtests',json={**OPTIONS,**changes}).status_code==422


def test_api_reports_immutable_and_do_not_promote_models(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    with main.db() as connection:
        seed_world(connection)
    first=client.post('/api/backtests',json=OPTIONS)
    assert first.status_code==200
    report=first.json()
    assert client.get('/api/backtests/'+str(report['id'])).json()==report
    second=client.post('/api/backtests',json={**OPTIONS,'min_probability':.99}).json()
    assert second['id']!=report['id']
    assert second['strategy']['financial']['bets']==0
    assert client.get('/api/backtests/'+str(report['id'])).json()==report
    assert client.get('/api/backtests').json()['total']==2
    assert client.get('/api/dashboard').json()['selections']==[]
    assert client.get('/api/backtests/9999').status_code==404


def test_sast_periods_classify_decision_time_not_kickoff_date(client):
    with main.db() as connection:
        # Kickoff is 00:30 SAST on 2 July; the decision was 23:30 on 1 July.
        add_result(connection,'Synthetic 0','Synthetic 1',datetime(2026,7,1,22,30,tzinfo=timezone.utc),2,0)
        report=backtesting.run(connection,{**OPTIONS,'calibration_start':'2026-07-01','test_start':'2026-07-02','test_end':'2026-07-02'},NOW)
    assert report['coverage']['calibration_targets']==1 and report['coverage']['test_targets']==0
    assert report['entries'][0]['truth'] is None  # Result was not known at the freeze.


def test_calibrated_strategy_never_silently_falls_back_to_raw(client):
    with main.db() as connection:
        ids=seed_world(connection)
        for match_id in ids[:24]:
            connection.execute('UPDATE historical_observations SET imported_at=? WHERE match_id=?',(NOW.isoformat(),match_id))
        report=backtesting.run(connection,{**OPTIONS,'strategy_probabilities':'calibrated_1x2'},NOW)
    assert report['models']['poisson']['raw']['sample']==8
    assert report['models']['poisson']['calibration_fit']['temperature'] is None
    assert report['strategy']['financial']['bets']==0
    assert report['strategy']['excluded']['calibration_unavailable']==8


def test_draw_no_bet_pushes_return_stake_and_exclude_push_win_rate(client):
    with main.db() as connection:
        seed_world(connection,quote_market='draw_no_bet',quote_price=1.8)
        report=backtesting.run(connection,{**OPTIONS,'market':'draw_no_bet'},NOW)
    financial=report['strategy']['financial']
    assert financial['bets']==8 and financial['pushes']==2
    assert financial['net_profit']==pytest.approx(1.2)
    assert financial['win_rate']==pytest.approx(4/6)
    assert report['strategy']['probability_metrics']['sample']==6


def test_target_limit_fails_before_fitting_or_saving_partial_report(client,monkeypatch):
    monkeypatch.setattr(main,'now',lambda:NOW)
    with main.db() as connection:
        for i in range(101):
            add_result(connection,'Synthetic 0','Synthetic 1',datetime(2026,7,1,16,tzinfo=timezone.utc)+timedelta(minutes=i),1,1)
    response=client.post('/api/backtests',json={**OPTIONS,'method':'walk_forward'})
    assert response.status_code==422 and '100' in response.json()['detail']
    assert client.get('/api/backtests').json()['total']==0


def test_replay_cannot_use_a_fixture_schedule_learned_after_decision(client):
    with main.db() as connection:
        seed_world(connection,warmup=True)
        connection.execute('DELETE FROM fixture_observations')
        report=backtesting.run(connection,{**OPTIONS,'method':'walk_forward'},NOW)
    assert report['status']=='no_evaluable_predictions'
    assert 'scheduled fixture identity' in report['entries'][-1]['unavailable']['all']


@pytest.mark.parametrize('minutes_before',[65,70])
def test_postponement_known_before_decision_withholds_saved_forecast(client,minutes_before):
    with main.db() as connection:
        ids=seed_world(connection)
        target=connection.execute('SELECT * FROM historical_matches WHERE id=?',(ids[-1],)).fetchone()
        # More recent than the saved prediction but still before the decision.
        at=(datetime.fromisoformat(target['kickoff_utc'])-timedelta(minutes=minutes_before)).isoformat()
        fixture=connection.execute('SELECT id FROM fixtures WHERE identity=?',(target['identity'],)).fetchone()
        connection.execute('INSERT INTO fixture_observations(fixture_id,identity,status,recorded_at,source) VALUES (?,?,?,?,?)',
            (fixture['id'],target['identity'],'postponed',at,'Synthetic postponement'))
        report=backtesting.run(connection,OPTIONS.copy(),NOW)
    assert report['models']['poisson']['raw']['sample']==7
    assert 'not scheduled' in report['entries'][-1]['unavailable']['all']


def test_fixture_status_history_and_conservative_migration(client):
    kickoff=(main.now()+timedelta(days=1)).astimezone(history.SAST).isoformat()
    csv='home,away,competition,kickoff_sast,status\nSynthetic Home,Synthetic Away,Synthetic League,'+kickoff+',scheduled'
    client.post('/api/fixtures/import',json={'csv':csv})
    client.post('/api/fixtures/import',json={'csv':csv})
    client.post('/api/fixtures/import',json={'csv':csv.replace('scheduled','postponed')})
    with main.db() as connection:
        assert [r['status'] for r in connection.execute('SELECT status FROM fixture_observations ORDER BY id')]==['scheduled','postponed']
        # Simulate an older installation with only its latest fixture row.
        connection.execute('DELETE FROM fixture_observations')
    main.initialize()
    with main.db() as connection:
        states=connection.execute('SELECT * FROM fixture_observations').fetchall()
        assert len(states)==1 and states[0]['status']=='postponed'
        current=connection.execute('SELECT imported_at FROM fixtures').fetchone()[0]
        assert states[0]['recorded_at']==current


@pytest.mark.parametrize('half_life,count',[(90,2),(180,1)])
def test_calibration_configuration_identity(client,half_life,count):
    with main.db() as connection:
        seed_world(connection)
        row=connection.execute('SELECT id,result FROM model_runs ORDER BY id DESC LIMIT 1').fetchone()
        stored=json.loads(row['result'])
        stored['options']['half_life_days']=half_life
        stored['options']['ridge']=1  # Numerically identical to 1.0 is not a change.
        connection.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps(stored),row['id']))
        report=backtesting.run(connection,OPTIONS.copy(),NOW)
    assert report['models']['poisson']['raw']['sample']==8
    assert report['models']['poisson']['original_configuration_count']==count
    if count==2:
        assert report['models']['poisson']['calibration_fit']['temperature'] is None
        assert 'settings differ' in report['models']['poisson']['calibration_fit']['reason']
    else:
        assert report['models']['poisson']['calibration_fit']['temperature'] is not None
