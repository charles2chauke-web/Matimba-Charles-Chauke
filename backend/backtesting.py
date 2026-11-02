"""Strict chronological research replay and immutable saved-prediction evaluation."""
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from itertools import combinations

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.special import logsumexp

from backend import history, models, odds

NAMES=('poisson','dixon_coles','xg_poisson')
VERSION='backtest-research-0.1.0'


def initialize(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS backtest_runs (
      id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL)''')


def period(value):
    return datetime.combine(date.fromisoformat(value),time.min,tzinfo=history.SAST).astimezone(timezone.utc)


def validate_options(options,current):
    if not history.normalized(options['competition']):
        raise ValueError('Competition name must not be blank')
    calibration_start,test_start,end=period(options['calibration_start']),period(options['test_start']),period(options['test_end'])+timedelta(days=1)
    if not calibration_start<test_start<end:
        raise ValueError('Require calibration_start < test_start <= test_end (SAST decision dates)')
    if date.fromisoformat(options['test_end'])>current.astimezone(history.SAST).date():
        raise ValueError('Holdout end cannot be a future SAST date')
    market,selection,line=options['market'],options['selection'],options['line']
    if market not in odds.OUTCOMES or selection not in odds.OUTCOMES[market]:
        raise ValueError('Unsupported strategy market/selection')
    allowed={'.5','0.5','1.5','2.5','3.5'} if market=='goals' else {'.5','0.5'} if market.startswith('team_goals_') else None
    if (allowed is None and line) or (allowed is not None and line not in allowed):
        raise ValueError('Unsupported strategy line; other markets require an empty line')
    if options['strategy_probabilities']=='calibrated_1x2' and market not in ('1x2','double_chance','draw_no_bet'):
        raise ValueError('Temperature-calibrated strategy probabilities support only 1x2, double_chance and draw_no_bet')
    if options['strategy_probabilities']=='calibrated_1x2' and options['calibration']=='none':
        raise ValueError('Enable temperature calibration before selecting calibrated strategy probabilities')
    if allowed is not None:
        options['line']=str(float(line))
    return calibration_start,test_start,end


def probability_vector(model):
    p=np.array([model['result'][key] for key in ('home','draw','away')],dtype=float)
    if not np.all(np.isfinite(p)) or np.any(p<0) or np.any(p>1) or abs(float(p.sum())-1)>1e-8:
        raise ValueError('Invalid saved outcome probabilities')
    return p


def temperature_scale(probabilities,temperature):
    logits=np.log(np.maximum(np.asarray(probabilities,dtype=float),1e-15))/temperature
    return np.exp(logits-logsumexp(logits,axis=-1,keepdims=True))


def calibration_bins(probabilities,outcomes):
    bins=[]
    for i in range(10):
        selected=[(p,y) for p,y in zip(probabilities,outcomes) if min(int(p*10),9)==i]
        bins.append({'lower':i/10,'upper':(i+1)/10,'count':len(selected),
                     'mean_probability':sum(p for p,_ in selected)/len(selected) if selected else None,
                     'observed_rate':sum(y for _,y in selected)/len(selected) if selected else None})
    return bins


def predictive_metrics(probabilities,outcomes):
    if not probabilities:
        return {'sample':0,'accuracy':None,'brier':None,'log_loss':None,'ece':None,'calibration':{}}
    p=np.asarray(probabilities,dtype=float)
    y=np.asarray(outcomes,dtype=int)
    target=np.eye(3)[y]
    curves={name:calibration_bins(p[:,i].tolist(),target[:,i].tolist()) for i,name in enumerate(('home','draw','away'))}
    ece=sum(sum(b['count']/len(y)*abs(b['mean_probability']-b['observed_rate']) for b in bins if b['count']) for bins in curves.values())/3
    return {'sample':len(y),'accuracy':float(np.mean(p.argmax(axis=1)==y)),
            'brier':float(np.square(p-target).sum(axis=1).mean()),
            'log_loss':float(-np.log(np.maximum(p[np.arange(len(y)),y],1e-15)).mean()),
            'ece':ece,'calibration':curves}


def fit_temperature(probabilities,outcomes):
    if len(outcomes)<20:
        return {'status':'insufficient_data','sample':len(outcomes),'temperature':None,'reason':'At least 20 chronologically eligible calibration outcomes are required'}
    if len(set(outcomes))<2:
        return {'status':'insufficient_data','sample':len(outcomes),'temperature':None,'reason':'Calibration requires at least two observed outcome classes'}
    p=np.asarray(probabilities,dtype=float)
    y=np.asarray(outcomes,dtype=int)
    def loss(log_t):
        adjusted=temperature_scale(p,math.exp(log_t))
        return float(-np.log(np.maximum(adjusted[np.arange(len(y)),y],1e-15)).mean())
    low,high=math.log(.25),math.log(4)
    fit=minimize_scalar(loss,bounds=(low,high),method='bounded',options={'xatol':1e-8})
    if not fit.success or min(fit.x-low,high-fit.x)<1e-5:
        return {'status':'withheld','sample':len(y),'temperature':None,'reason':'Calibration optimisation failed or reached its research boundary'}
    return {'status':'fitted_for_research','sample':len(y),'temperature':math.exp(float(fit.x)),
            'reason':'Fitted only on calibration-period outcomes known before the holdout began; no deployment approval'}


def configuration_signature(entry):
    settings=entry.get('source_model_options')
    canonical=None if settings is None else {key:float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) else value
                                            for key,value in settings.items()}
    minimum=entry.get('source_minimum_sample')
    return json.dumps([entry.get('model_version'),canonical,float(minimum) if minimum is not None else None],sort_keys=True)


def result_evidence(connection,target,cutoff):
    items=history.observations(connection,target['id'],cutoff.isoformat())
    resolved=history.resolve(items)
    home,away=resolved['metrics']['goals_home'],resolved['metrics']['goals_away']
    if home is None or away is None:
        return None
    return {'home_goals':home,'away_goals':away,'outcome':0 if home>away else 1 if home==away else 2,
            'available_at':max(item['available_at'] for item in items),
            'observation_ids':[item['id'] for item in items],
            'reliability':'Manual result sources unverified'}


def settlement(market,selection,line,home,away):
    outcome='home' if home>away else 'draw' if home==away else 'away'
    if market=='draw_no_bet':
        return 'push' if outcome=='draw' else 'win' if outcome==selection else 'loss'
    if market=='1x2':
        won=outcome==selection
    elif market=='double_chance':
        won=outcome in {'home_or_draw':('home','draw'),'away_or_draw':('away','draw'),'home_or_away':('home','away')}[selection]
    elif market=='btts':
        won=(home>0 and away>0)==(selection=='yes')
    else:
        total=home+away if market=='goals' else home if market=='team_goals_home' else away
        won=total>float(line) if selection=='over' else total<float(line)
    return 'win' if won else 'loss'


def strategy_probability(model,options,temperature):
    market,selection=options['market'],options['selection']
    if options['strategy_probabilities']=='calibrated_1x2':
        if temperature is None:
            return None
        h,d,a=temperature_scale(probability_vector(model),temperature).tolist()
        if market=='1x2':
            return {'probability':{'home':h,'draw':d,'away':a}[selection],'push':0.}
        if market=='double_chance':
            return {'probability':{'home_or_draw':h+d,'away_or_draw':a+d,'home_or_away':h+a}[selection],'push':0.}
        return {'probability':h if selection=='home' else a,'push':d}
    return next({'probability':m['probability'],'push':m['push']} for m in odds.model_markets(model)
        if (m['market'],m['selection'],m['line'])==(market,selection,options['line']))


def historical_price(connection,target,cutoff,options):
    rows=connection.execute('''SELECT * FROM odds_observations WHERE fixture_identity=? AND market=?
      AND selection=? AND line=? AND settlement=? AND observed_at<=? AND imported_at<=?
      ORDER BY observed_at,id LIMIT 10001''',(target['identity'],options['market'],options['selection'],
      options['line'],odds.SETTLEMENT,cutoff.isoformat(),cutoff.isoformat())).fetchall()
    if len(rows)>10000:
        raise ValueError('Historical price evidence exceeds the 10,000-observation evaluation limit')
    resolved=odds.resolve_quotes([dict(row) for row in rows],cutoff,options['max_odds_age_minutes'])
    eligible=[quote for quote in resolved if quote['status']=='fresh']
    return max(eligible,key=lambda q:q['decimal_odds']) if eligible else None


def financial_metrics(trades):
    if not trades:
        return {'bets':0,'wins':0,'losses':0,'pushes':0,'staked':0.,'net_profit':None,'roi':None,'win_rate':None,
                'max_drawdown_units':None,'profit_curve':[]}
    cumulative=peak=max_drawdown=0.
    curve=[]
    for trade in sorted(trades,key=lambda t:(t['settled_at'],t['profit'],t['decision_at'],t['match_id'])):
        cumulative+=trade['profit']
        peak=max(peak,cumulative)
        max_drawdown=max(max_drawdown,peak-cumulative)
        curve.append({'at':trade['settled_at'],'match_id':trade['match_id'],'profit':cumulative,'drawdown':peak-cumulative})
    wins=sum(t['outcome']=='win' for t in trades)
    losses=sum(t['outcome']=='loss' for t in trades)
    staked=sum(t['stake'] for t in trades)
    return {'bets':len(trades),'wins':wins,'losses':losses,'pushes':len(trades)-wins-losses,
            'staked':staked,'net_profit':cumulative,'roi':cumulative/staked,
            'win_rate':wins/(wins+losses) if wins+losses else None,
            'max_drawdown_units':max_drawdown,'profit_curve':curve}


def load_saved_runs(connection,start,end):
    rows=connection.execute('SELECT id,result FROM model_runs WHERE generated_at>=? AND generated_at<? ORDER BY id LIMIT 5001',
                            (start.isoformat(),end.isoformat())).fetchall()
    if len(rows)>5000:
        raise ValueError('Saved prediction audit exceeds 5,000 candidate runs; narrow the period')
    grouped=defaultdict(list)
    for row in rows:
        run={'id':row['id'],**json.loads(row['result'])}
        grouped[odds.fixture_identity(run['fixture'])].append(run)
    return grouped


def forecast(connection,target,cutoff,options,saved):
    known_fixture=connection.execute('''SELECT id,status,recorded_at FROM fixture_observations
        WHERE identity=? AND recorded_at<=? ORDER BY recorded_at DESC,id DESC LIMIT 1''',
        (target['identity'],cutoff.isoformat())).fetchone()
    if options['method']=='saved_predictions':
        candidates=[run for run in saved.get(target['identity'],[]) if datetime.fromisoformat(run['generated_at'])<=cutoff]
        if not candidates:
            return {'models':{},'unavailable':{'all':'No stored prediction existed by the decision cutoff'},'source_run_id':None}
        run=max(candidates,key=lambda r:(r['generated_at'],r['id']))
        if run['fixture']['status']!='scheduled' or datetime.fromisoformat(run['generated_at'])>=datetime.fromisoformat(target['kickoff_utc']):
            return {'models':{},'unavailable':{'all':'Stored snapshot was not a scheduled pre-kickoff prediction'},'source_run_id':run['id']}
        if known_fixture and known_fixture['recorded_at']>=run['generated_at'] and known_fixture['status']!='scheduled':
            return {'models':{},'unavailable':{'all':'A later fixture observation was not scheduled at the decision cutoff'},'source_run_id':run['id'],'fixture_observation_id':known_fixture['id']}
        if (cutoff-datetime.fromisoformat(run['generated_at'])).total_seconds()/3600>options['max_model_age_hours']:
            return {'models':{},'unavailable':{'all':'Latest eligible stored prediction exceeds the model-age limit'},'source_run_id':run['id']}
        if datetime.fromisoformat(run['training_cutoff'])>datetime.fromisoformat(run['generated_at']):
            return {'models':{},'unavailable':{'all':'Saved training cutoff is later than prediction time'},'source_run_id':run['id']}
        return {'models':run['models'],'unavailable':run['unavailable'],'source_run_id':run['id'],
                'model_version':run['model_version'],'forecast_at':run['generated_at'],
                'source_model_options':run.get('options'),'source_minimum_sample':run.get('minimum_sample'),
                'training_fingerprint':run['dataset_fingerprint'],'training_observation_ids':run['observation_ids']}
    if known_fixture is None or known_fixture['status']!='scheduled':
        return {'models':{},'unavailable':{'all':'No scheduled fixture identity was recorded by the historical decision cutoff'},
                'source_run_id':None,'fixture_observation_id':known_fixture['id'] if known_fixture else None}
    try:
        rows,fingerprint,evidence=models.load_dataset(connection,target['competition'],cutoff,options['model_options']['training_days'])
        analysis=models.analyse(rows,target,cutoff,options['model_options'],options['minimum_sample'],include_context=False)
    except models.ModelUnavailable as error:
        return {'models':{},'unavailable':{'all':str(error)},'source_run_id':None}
    return {'models':analysis['models'],'unavailable':analysis['unavailable'],'source_run_id':None,
            'model_version':models.VERSION,'forecast_at':cutoff.isoformat(),
            'source_model_options':options['model_options'],'source_minimum_sample':options['minimum_sample'],
            'training_fingerprint':fingerprint,'training_observation_ids':evidence,'fixture_observation_id':known_fixture['id']}


def run(connection,options,current):
    calibration_start,test_start,end=validate_options(options,current)
    lead=timedelta(minutes=options['decision_lead_minutes'])
    targets=connection.execute('''SELECT * FROM historical_matches WHERE competition_key=?
        AND kickoff_utc>=? AND kickoff_utc<? AND kickoff_utc<? ORDER BY kickoff_utc,id LIMIT 101''',
        (history.normalized(options['competition']),(calibration_start+lead).isoformat(),
         (end+lead).isoformat(),current.isoformat())).fetchall()
    if len(targets)>100:
        raise ValueError('More than 100 calibration/holdout targets; narrow the SAST decision-date windows')
    saved=load_saved_runs(connection,calibration_start-timedelta(hours=options['max_model_age_hours']),end) if targets and options['method']=='saved_predictions' else {}
    entries=[]
    for target_row in targets:
        target=dict(target_row)
        cutoff=datetime.fromisoformat(target['kickoff_utc'])-lead
        phase='calibration' if cutoff<test_start else 'test'
        truth=result_evidence(connection,target,test_start if phase=='calibration' else current)
        entry={'match_id':target['id'],'home':target['home'],'away':target['away'],'competition':target['competition'],
               'kickoff_utc':target['kickoff_utc'],'decision_at':cutoff.isoformat(),'phase':phase,
               'truth':truth,'models':{},'unavailable':{},'status':'unavailable'}
        if truth is None:
            entry['unavailable']={'all':'No non-conflicting result known by calibration freeze' if phase=='calibration' else 'Missing or conflicting final result'}
        else:
            predicted=forecast(connection,target,cutoff,options,saved)
            for model in predicted['models'].values():
                probability_vector(model)
            entry.update(predicted)
            entry['status']='evaluated' if entry['models'] else 'unavailable'
        entry['identity']=target['identity']
        entries.append(entry)
    model_reports={}
    calibrators={}
    for name in NAMES:
        calibration=[e for e in entries if e['phase']=='calibration' and name in e['models']]
        test=[e for e in entries if e['phase']=='test' and name in e['models']]
        calib_p=[probability_vector(e['models'][name]).tolist() for e in calibration]
        calib_y=[e['truth']['outcome'] for e in calibration]
        configurations={configuration_signature(e) for e in calibration+test}
        unknown=any(e.get('source_model_options') is None or e.get('source_minimum_sample') is None for e in calibration+test)
        if options['calibration']=='none':
            fitted={'status':'disabled','sample':len(calibration),'temperature':None,'reason':'Calibration disabled in study options'}
        elif len(configurations)>1 or unknown:
            fitted={'status':'withheld','sample':len(calibration),'temperature':None,
                'reason':'Original model versions/settings differ or are unknown; narrow the audit or replay one fixed configuration'}
        else:
            fitted=fit_temperature(calib_p,calib_y)
        fitted['frozen_at']=test_start.isoformat()
        fitted['match_ids']=[e['match_id'] for e in calibration]
        calibrators[name]=fitted
        p=[probability_vector(e['models'][name]).tolist() for e in test]
        y=[e['truth']['outcome'] for e in test]
        temperature=fitted['temperature']
        model_reports[name]={'calibration_fit':fitted,'raw':predictive_metrics(p,y),
            'original_configuration_count':len(configurations),
            'calibrated':predictive_metrics(temperature_scale(np.asarray(p),temperature).tolist(),y) if temperature is not None and p else None,
            'holdout_match_ids':[e['match_id'] for e in test]}
    pairwise=[]
    for first,second in combinations(NAMES,2):
        common=[e for e in entries if e['phase']=='test' and first in e['models'] and second in e['models']]
        actual=[e['truth']['outcome'] for e in common]
        pairwise.append({'first':first,'second':second,'sample':len(common),
            'first_metrics':predictive_metrics([probability_vector(e['models'][first]).tolist() for e in common],actual),
            'second_metrics':predictive_metrics([probability_vector(e['models'][second]).tolist() for e in common],actual)})
    trades=[]
    strategy_points=[]
    omitted=Counter()
    selected_model=options['strategy_model']
    for entry in entries:
        if entry['phase']!='test':
            continue
        if selected_model not in entry['models']:
            omitted['model_or_result_unavailable']+=1
            continue
        probability=strategy_probability(entry['models'][selected_model],options,calibrators[selected_model]['temperature'])
        if probability is None:
            omitted['calibration_unavailable']+=1
            continue
        truth=entry['truth']
        outcome=settlement(options['market'],options['selection'],options['line'],truth['home_goals'],truth['away_goals'])
        if outcome!='push':
            conditional=probability['probability']/(1-probability['push']) if probability['push']<1 else None
            if conditional is not None:
                strategy_points.append((conditional,int(outcome=='win')))
        price=historical_price(connection,entry,datetime.fromisoformat(entry['decision_at']),options)
        if price is None:
            omitted['no_fresh_historical_odds']+=1
            continue
        value=odds.calculate(probability['probability'],price['decimal_odds'],probability['push'])
        if probability['probability']<options['min_probability']:
            omitted['below_probability_filter']+=1
            continue
        if value['expected_value']<options['min_ev']:
            omitted['below_ev_filter']+=1
            continue
        stake=options['flat_stake']
        profit=stake*(price['decimal_odds']-1) if outcome=='win' else 0. if outcome=='push' else -stake
        trades.append({'match_id':entry['match_id'],'home':entry['home'],'away':entry['away'],
            'competition':entry['competition'],'decision_at':entry['decision_at'],'settled_at':truth['available_at'],
            'market':options['market'],'selection':options['selection'],'line':options['line'],
            'model':selected_model,'probability':probability['probability'],'push_probability':probability['push'],
            'decimal_odds':price['decimal_odds'],'bookmaker':price['bookmaker'],'expected_value':value['expected_value'],
            'odds_observation_ids':[o['id'] for o in price['source_observations']],
            'outcome':outcome,'stake':stake,'profit':profit,'source_run_id':entry.get('source_run_id')})
    binary_p=[p for p,_ in strategy_points];binary_y=[y for _,y in strategy_points]
    strategy_metrics={'sample':len(strategy_points),'basis':'Conditional non-push outcomes; all eligible selected-model holdout forecasts, before betting filters',
        'brier':sum((p-y)**2 for p,y in strategy_points)/len(strategy_points) if strategy_points else None,
        'log_loss':-sum(math.log(max(p if y else 1-p,1e-15)) for p,y in strategy_points)/len(strategy_points) if strategy_points else None,
        'calibration':calibration_bins(binary_p,binary_y)}
    grouped={}
    for field in ('competition','market'):
        groups=defaultdict(list)
        for trade in trades:
            groups[trade[field]].append(trade)
        grouped[field]={key:{k:v for k,v in financial_metrics(group).items() if k!='profit_curve'} for key,group in groups.items()}
    buckets=defaultdict(list)
    for trade in trades:
        index=min(int(trade['probability']*10),9)
        buckets[f'{index*10}–{(index+1)*10}%'].append(trade)
    grouped['probability_range']={key:{k:v for k,v in financial_metrics(group).items() if k!='profit_curve'} for key,group in sorted(buckets.items())}
    total_test=sum(report['raw']['sample'] for report in model_reports.values())
    report={'version':VERSION,'created_at':current.isoformat(),'status':'completed_research' if total_test else 'no_evaluable_predictions',
        'options':options,'calibration_freeze':test_start.isoformat(),'models':model_reports,'pairwise':pairwise,
        'coverage':{'targets':len(entries),'calibration_targets':sum(e['phase']=='calibration' for e in entries),
                    'test_targets':sum(e['phase']=='test' for e in entries),'unavailable_targets':sum(e['status']=='unavailable' for e in entries)},
        'strategy':{'probability_metrics':strategy_metrics,'financial':financial_metrics(trades),'excluded':dict(omitted),'groups':grouped,'trades':trades,
                    'closing_line_value':None,'drawdown_method':'Settlement evidence time; losses precede wins at tied timestamps (conservative within-batch ordering)'},
        'entries':entries,'deployment_approved':False,'recommendation':None,
        'warnings':[
            'Research study only. Manual data/odds and execution availability are unverified; no profitability or independent validation claim is established.',
            'Decision dates use SAST. Training uses only results both observed and received by each decision cutoff. Newly imported history cannot be backdated.',
            'Replay also requires a recorded scheduled fixture identity before each decision. Final schedules are not assumed to have been known earlier.',
            'Calibration uses only calibration-window labels known before the holdout begins. Holdout labels never fit temperature or model strengths for that decision.',
            'Study parameters are chosen retrospectively; repeated holdout inspection creates selection risk. No model or calibrator is automatically deployed.',
            'Saved-prediction audits can aggregate different original research settings. Temperature is withheld across mixed/unknown configurations; replay uses one fixed configuration.',
            'ROI uses actual eligible historical imported odds, never current replacements. No odds means no simulated bet; zero bets means ROI/profit unavailable.',
            'One fixed market/selection and model are used per study, at most one simulated selection per match. No accumulators, stake recovery or real-money betting.',
            'Flat-stake P&L excludes taxes/commission and bankroll/exposure limits. Drawdown uses conservative loss-first ordering when settlement timestamps tie.',
            'Closing-line value and quantified predictive uncertainty remain unavailable. No ensemble weights are learned.']}
    report['fingerprint']=hashlib.sha256(json.dumps(report,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return report
