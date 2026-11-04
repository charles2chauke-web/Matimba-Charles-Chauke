"""Server-side strict daily prediction contract; no client-side bypass."""
from datetime import datetime,timezone
from backend import history,odds

# Explicit, visible major-competition preset: top five European domestic
# leagues and UEFA Champions/Europa/Conference leagues. No forced selections.
MAJOR_LEAGUES={39,140,135,78,61,2,3,848}
MAJOR_NAMES=['Premier League','La Liga','Serie A','Bundesliga','Ligue 1',
             'UEFA Champions League','UEFA Europa League','UEFA Conference League']
MARKETS={('goals','over','1.5'),('goals','over','2.5'),('1x2','home',''),('1x2','away',''),
         ('corners','over','7.5'),('yellow_cards','over','2.5')}


def is_today(kickoff,current):
    value=datetime.fromisoformat(kickoff)
    return value>current and value.astimezone(history.SAST).date()==current.astimezone(history.SAST).date()


def policy():
    return {'today_only':True,'timezone':'Africa/Johannesburg','major_league_ids':sorted(MAJOR_LEAGUES),
            'major_competitions':MAJOR_NAMES,'allowed_markets':[list(k) for k in sorted(MARKETS)],
            'ranking':'Major competitions first when eligible; highest conservative model probability within each group.',
            'minimum_probability':.75,'maximum_model_disagreement':.10,
            'reliability':'Model stability and sample filters; not independently validated accuracy or a guarantee.'}


def candidates(snapshot,model):
    primary=snapshot.get('models',{}).get(model)
    other=snapshot.get('models',{}).get('dixon_coles' if model=='poisson' else 'poisson')
    comparison={(r['market'],r['selection'],r['line']):r for r in odds.model_markets(other)} if other else {}
    rows=[]
    if primary:
        for row in odds.model_markets(primary):
            key=(row['market'],row['selection'],row['line'])
            if key not in MARKETS:continue
            alt=comparison.get(key)
            if not alt:continue
            rows.append({**row,'comparison_probability':alt['probability'],
                         'diagnostics':primary['diagnostics'],'method':'Poisson / conditional Dixon–Coles stability check'})
    for model in snapshot.get('count_models',{}).values():
        for row in model.get('markets',[]):
            if (row['market'],row['selection'],row['line']) in MARKETS:
                rows.append({**row,'diagnostics':model['diagnostics'],'method':model['method']})
    return rows
