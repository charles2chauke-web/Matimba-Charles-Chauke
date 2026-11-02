"""Daily research shortlist. Research ranking never implies qualification."""
import json
from collections import Counter
from datetime import datetime,timedelta
from backend import odds


def research_order(row,minimum_ev):
    ev=row['expected_value']
    priced_value=ev is not None and ev>0 and ev>=minimum_ev
    return (priced_value,ev if priced_value else row['probability'],row['probability'])


def build(connection,settings,current,model='poisson'):
    rows=connection.execute("SELECT * FROM fixtures WHERE status IN ('scheduled','listed') AND kickoff_utc>? ORDER BY kickoff_utc,id",(current.isoformat(),)).fetchall()
    in_window=[]
    for row in rows:
        kickoff=datetime.fromisoformat(row['kickoff_utc'])
        if (settings['window']=='24h' and kickoff<current+timedelta(hours=24)) or (settings['window']=='today' and kickoff.astimezone(current.tzinfo).date()==current.date()):
            in_window.append(dict(row))
    candidates=[];excluded=[]
    for fixture in in_window:
        saved=connection.execute('SELECT id,result FROM model_runs WHERE fixture_id=? ORDER BY generated_at DESC,id DESC LIMIT 1',(fixture['id'],)).fetchone()
        def omit(reason):excluded.append({'fixture_id':fixture['id'],'home':fixture['home'],'away':fixture['away'],'reason':reason})
        if saved is None:
            omit('No saved model analysis. Run the data-and-model workflow first.');continue
        snapshot={'id':saved['id'],**json.loads(saved['result'])}
        if model not in snapshot['models']:
            omit('Selected model unavailable in the latest analysis; inspect its coverage report.');continue
        if odds.fixture_identity(snapshot['fixture'])!=odds.fixture_identity(fixture):
            omit('Fixture changed since analysis; generate a new snapshot.');continue
        generated=datetime.fromisoformat(snapshot['generated_at'])
        if generated>current or current-generated>timedelta(hours=24):
            omit('Model snapshot is outside the 24-hour research freshness window.');continue
        diagnostics=snapshot['models'][model].get('diagnostics',{})
        minimum=max(30,settings['min_sample'])
        if diagnostics.get('sample',0)<minimum or diagnostics.get('effective_sample',0)<minimum:
            omit('Historical sample is below the saved research minimum.');continue
        values=odds.assess(connection,snapshot,model,current,current,{'pricing_time':'current','model':model,'max_odds_age_minutes':60,'max_model_age_hours':24})
        eligible=[r for r in values['rows'] if r['probability']>=settings['min_probability']]
        if not eligible:
            omit('No market reaches your minimum probability; estimates were not adjusted.');continue
        # Positive-EV rows take precedence only when a genuine imported price exists.
        # Without one, ranking is explicitly probability-only research, not value.
        eligible.sort(key=lambda r:research_order(r,settings['min_ev']),reverse=True)
        best=eligible[0]
        blockers=['Model has no independently approved calibration/deployment evidence.',
                  'Validated predictive uncertainty is unavailable.',
                  'Source statistics and fixture status have not been independently verified.']
        if fixture['status']!='scheduled':blockers.append('Fixture is listed/unconfirmed, not verified scheduled.')
        if best['best_quote'] is None:blockers.append('No fresh, non-conflicting bookmaker odds; betting value is unverified.')
        elif best['expected_value'] is None or best['expected_value']<settings['min_ev']:blockers.append('Estimated value does not meet the saved EV requirement.')
        candidates.append({'fixture_id':fixture['id'],'run_id':saved['id'],'home':fixture['home'],'away':fixture['away'],
            'competition':fixture['competition'],'kickoff_sast':datetime.fromisoformat(fixture['kickoff_utc']).astimezone(current.tzinfo).isoformat(),
            'fixture_status':fixture['status'],'model':model,'model_generated_at':snapshot['generated_at'],
            'market':best['market'],'selection':best['selection'],'line':best['line'],'probability':best['probability'],
            'push_probability':best['push_probability'],'fair_odds':best['fair_odds'],
            'decimal_odds':best['best_quote']['decimal_odds'] if best['best_quote'] else None,'expected_value':best['expected_value'],
            'sample':diagnostics['sample'],'effective_sample':diagnostics['effective_sample'],
            'data_quality':'Unverified source · research only','qualification_blockers':blockers,'qualified':False,
            'reasons':[f"{diagnostics['sample']} historical matches; effective time-weighted sample {diagnostics['effective_sample']:.1f}.",
                       'One primary research market per match. No dependent probabilities were multiplied.',
                       'Fresh priced value takes priority; otherwise ordering is probability-only, not a value recommendation.']})
    candidates.sort(key=lambda r:research_order(r,settings['min_ev']),reverse=True)
    counts=Counter();watchlist=[]
    for candidate in candidates:
        if counts[candidate['competition']]>=settings['max_per_league']:
            excluded.append({'fixture_id':candidate['fixture_id'],'home':candidate['home'],'away':candidate['away'],'reason':'Maximum selections per league reached; no extra correlated exposure was added.'});continue
        if len(watchlist)>=5:
            excluded.append({'fixture_id':candidate['fixture_id'],'home':candidate['home'],'away':candidate['away'],'reason':'Outside the five-place research shortlist.'});continue
        counts[candidate['competition']]+=1
        watchlist.append({'rank':len(watchlist)+1,**candidate})
    return {'generated_at':current.isoformat(),'timezone':'Africa/Johannesburg','window':settings['window'],'model':model,
            'qualified':[],'qualified_count':0,'research_watchlist':watchlist,'fixtures_in_window':len(in_window),
            'excluded':excluded,'settings':{k:settings[k] for k in ('min_probability','min_ev','min_sample','max_per_league')},
            'message':f'{len(watchlist)} research candidates met the probability/sample/diversification filters; 0 qualified betting selections.',
            'qualification_available':False,
            'qualification_explanation':'Production qualification remains unavailable until independent calibration, uncertainty and source-validation evidence is implemented. Research candidates never bypass those requirements.'}
