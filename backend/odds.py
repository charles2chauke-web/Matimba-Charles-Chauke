"""Append-only manual odds evidence and settlement-aware research calculations."""
import csv
import hashlib
import io
import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from backend import history

HEADER = ['fixture_id','bookmaker','market','selection','line','decimal_odds','observed_at','settlement']
SETTLEMENT = 'regulation_90'
OUTCOMES = {'1x2': ('home','draw','away'), 'goals': ('over','under'), 'btts': ('yes','no'),
            'team_goals_home': ('over','under'), 'team_goals_away': ('over','under'),
            'double_chance': ('home_or_draw','away_or_draw','home_or_away'), 'draw_no_bet': ('home','away')}


def initialize(connection):
    connection.executescript('''
    CREATE TABLE IF NOT EXISTS odds_observations (
      id INTEGER PRIMARY KEY, fixture_id INTEGER NOT NULL, fixture_identity TEXT NOT NULL,
      bookmaker TEXT NOT NULL, bookmaker_key TEXT NOT NULL, market TEXT NOT NULL,
      selection TEXT NOT NULL, line TEXT NOT NULL, decimal_odds REAL NOT NULL,
      observed_at TEXT NOT NULL, imported_at TEXT NOT NULL, settlement TEXT NOT NULL,
      source TEXT NOT NULL, source_url TEXT NOT NULL, digest TEXT NOT NULL UNIQUE);
    CREATE INDEX IF NOT EXISTS odds_fixture_time ON odds_observations(fixture_id,observed_at);
    CREATE TABLE IF NOT EXISTS value_assessments (
      id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, fixture_id INTEGER NOT NULL,
      created_at TEXT NOT NULL, pricing_cutoff TEXT NOT NULL, result TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS values_run_time ON value_assessments(run_id,created_at);
    ''')


def fixture_identity(fixture):
    kickoff = fixture.get('kickoff_utc') or fixture['kickoff_sast']
    kickoff = datetime.fromisoformat(kickoff).astimezone(timezone.utc).isoformat()
    return json.dumps([history.normalized(fixture[k]) for k in ('home','away','competition')]+[kickoff])


def parse(content, source, source_url, current, fixtures):
    if len(content.encode('utf-8'))>1_000_000:
        raise ValueError('Odds CSV must be 1 MB or smaller in UTF-8')
    source, source_url = history.source_metadata(source,source_url)
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
    if reader.fieldnames != HEADER:
        raise ValueError('Expected odds CSV header: '+','.join(HEADER))
    result = []
    for index,row in enumerate(reader,2):
        try:
            if index > 5001:
                raise ValueError('Maximum 5,000 odds observations per import')
            if None in row or any(value is None for value in row.values()):
                raise ValueError('Column count does not match the header')
            row={key:value.strip() for key,value in row.items()}
            try:
                fixture_id=int(row['fixture_id'])
            except ValueError:
                raise ValueError('fixture_id must be an integer from the fixture library') from None
            fixture=fixtures.get(fixture_id)
            if fixture is None:
                raise ValueError('Unknown fixture_id; import the fixture first')
            bookmaker=row['bookmaker']
            if not bookmaker or len(bookmaker)>100 or any(ord(c)<32 for c in bookmaker):
                raise ValueError('Bookmaker must contain 1–100 printable characters')
            market,selection=row['market'],row['selection']
            if market not in OUTCOMES or selection not in OUTCOMES[market]:
                raise ValueError('Unsupported market or selection; consult the odds import guide')
            line=row['line']
            if market=='goals':
                allowed={.5,1.5,2.5,3.5}
            elif market.startswith('team_goals_'):
                allowed={.5}
            else:
                allowed=None
            if allowed is not None:
                try:
                    number=float(line)
                except ValueError:
                    raise ValueError('This market requires a supported numeric line') from None
                if number not in allowed:
                    raise ValueError('Unsupported line for this market')
                line=str(number)
            elif line:
                raise ValueError('This market requires an empty line column')
            try:
                price=float(row['decimal_odds'])
            except ValueError:
                raise ValueError('decimal_odds must be numeric') from None
            if not math.isfinite(price) or not 1<price<=10000:
                raise ValueError('Decimal odds must be finite, greater than 1 and at most 10,000')
            if row['settlement']!=SETTLEMENT:
                raise ValueError('Only regulation_90 settlement is supported (90 minutes plus stoppage, no extra time/penalties)')
            observed=history.utc_datetime(row['observed_at'],'observed_at',current)
            if observed>=datetime.fromisoformat(fixture['kickoff_utc']):
                raise ValueError('Pre-match odds observation must precede fixture kickoff')
            identity=fixture_identity(fixture)
            digest=hashlib.sha256(json.dumps([identity,history.normalized(bookmaker),market,selection,line,price,
                observed.isoformat(),SETTLEMENT,history.normalized(source),source_url]).encode()).hexdigest()
            result.append({'fixture_id':fixture_id,'fixture_identity':identity,'bookmaker':bookmaker,
                'bookmaker_key':history.normalized(bookmaker),'market':market,'selection':selection,'line':line,
                'decimal_odds':price,'observed_at':observed.isoformat(),'imported_at':current.isoformat(),
                'settlement':SETTLEMENT,'source':source,'source_url':source_url,'digest':digest})
        except ValueError as error:
            raise ValueError(f'Row {index}: {error}') from None
    if not result:
        raise ValueError('CSV contains no odds observations')
    return result


def ingest(connection, rows):
    inserted=duplicates=0
    for row in rows:
        cursor=connection.execute('''INSERT OR IGNORE INTO odds_observations
          (fixture_id,fixture_identity,bookmaker,bookmaker_key,market,selection,line,decimal_odds,
           observed_at,imported_at,settlement,source,source_url,digest) VALUES
          (:fixture_id,:fixture_identity,:bookmaker,:bookmaker_key,:market,:selection,:line,:decimal_odds,
           :observed_at,:imported_at,:settlement,:source,:source_url,:digest)''',row)
        if cursor.rowcount:
            inserted+=1
        else:
            duplicates+=1
    return {'inserted':inserted,'duplicates':duplicates}


def calculate(probability, decimal_odds=None, push=0.):
    if any(not math.isfinite(value) or value<0 or value>1 for value in (probability,push)) or probability+push>1+1e-12:
        raise ValueError('Win/push probabilities must be finite, nonnegative and sum to at most one')
    if decimal_odds is not None and (not math.isfinite(decimal_odds) or decimal_odds<=1):
        raise ValueError('Decimal odds must be finite and greater than one')
    fair=(1-push)/probability if probability>0 else None
    conditional=probability/(1-push) if push<1 else None
    implied=1/decimal_odds if decimal_odds is not None else None
    return {'probability':probability,'push_probability':push,'fair_odds':fair,
            'conditional_probability':conditional,'implied_probability':implied,
            'probability_edge':conditional-implied if conditional is not None and implied is not None else None,
            'expected_value':probability*decimal_odds+push-1 if decimal_odds is not None else None}


def model_markets(model):
    rows=[]
    def add(market,selection,prob,line='',push=0.):
        rows.append({'market':market,'selection':selection,'line':line,'settlement':SETTLEMENT,
                     'probability':prob,'push':push})
    for selection,prob in model['result'].items():
        add('1x2',selection,prob)
    for key,prob in model['goals'].items():
        selection,line=key.split('_')
        add('goals',selection,prob,line)
    for selection,prob in model['btts'].items():
        add('btts',selection,prob)
    for side in ('home','away'):
        over=model['team_goals'][f'{side}_over_0.5']
        add('team_goals_'+side,'over',over,'0.5')
        add('team_goals_'+side,'under',1-over,'0.5')
    for selection,prob in model['double_chance'].items():
        add('double_chance',selection,prob)
    for side,states in model['draw_no_bet'].items():
        add('draw_no_bet',side,states['win'],push=states['push'])
    return rows


def observation_list(connection,fixture_id,limit,offset):
    count=connection.execute('SELECT COUNT(*) FROM odds_observations WHERE fixture_id=?',(fixture_id,)).fetchone()[0]
    rows=connection.execute('''SELECT id,fixture_id,bookmaker,market,selection,line,decimal_odds,observed_at,
        imported_at,settlement,source,source_url FROM odds_observations WHERE fixture_id=?
        ORDER BY observed_at DESC,id DESC LIMIT ? OFFSET ?''',(fixture_id,limit,offset)).fetchall()
    return {'total':count,'items':[dict(row) for row in rows],'limit':limit,'offset':offset,
            'reliability':'Manual odds export · source and availability unverified'}


def resolve_quotes(rows,cutoff,max_age_minutes):
    groups=defaultdict(list)
    for row in rows:
        key=(row['bookmaker_key'],row['market'],row['selection'],row['line'],row['settlement'])
        groups[key].append(row)
    resolved=[]
    for key,items in groups.items():
        latest=max(item['observed_at'] for item in items)
        current=[item for item in items if item['observed_at']==latest]
        prices={item['decimal_odds'] for item in current}
        age=(cutoff-datetime.fromisoformat(latest)).total_seconds()/60
        conflict=len(prices)>1
        state='conflicting' if conflict else 'stale' if age>max_age_minutes else 'fresh'
        quote={'bookmaker':current[0]['bookmaker'],'bookmaker_key':key[0],'market':key[1],'selection':key[2],
            'line':key[3],'settlement':key[4],'decimal_odds':next(iter(prices)) if not conflict else None,
            'status':state,'observed_at':latest,'age_minutes':age,
            'source_observations':[{'id':item['id'],'source':item['source'],'source_url':item['source_url'],
                'imported_at':item['imported_at'],'observed_at':item['observed_at'],'decimal_odds':item['decimal_odds']} for item in current],
            'first_recorded_odds':None,'first_recorded_at':None,'odds_change':None,
            'overround':None,'margin_removed_implied':None,'margin_status':'Incomplete or unsynchronised market'}
        earliest=min(item['observed_at'] for item in items)
        first_prices={item['decimal_odds'] for item in items if item['observed_at']==earliest}
        if len(first_prices)==1:
            quote['first_recorded_odds']=next(iter(first_prices))
            quote['first_recorded_at']=earliest
            if not conflict:
                quote['odds_change']=quote['decimal_odds']-quote['first_recorded_odds']
        resolved.append(quote)
    market_groups=defaultdict(list)
    for quote in resolved:
        market_groups[(quote['bookmaker_key'],quote['market'],quote['line'],quote['settlement'])].append(quote)
    for (_,market,_,_),quotes in market_groups.items():
        if market=='double_chance':
            for quote in quotes:
                quote['margin_status']='Not computed: double-chance outcomes overlap'
            continue
        if {q['selection'] for q in quotes}!=set(OUTCOMES[market]) or any(q['status']!='fresh' for q in quotes):
            continue
        times=[datetime.fromisoformat(q['observed_at']) for q in quotes]
        if (max(times)-min(times)).total_seconds()>60:
            continue
        total=sum(1/q['decimal_odds'] for q in quotes)
        for quote in quotes:
            quote['overround']=total-1
            quote['margin_removed_implied']=(1/quote['decimal_odds'])/total
            quote['margin_status']='Complete same-book market within 60 seconds; proportional margin removal assumption'
    return resolved


def assess(connection,snapshot,model_name,cutoff,created_at,options):
    identity=fixture_identity(snapshot['fixture'])
    query=connection.execute('''SELECT * FROM odds_observations WHERE fixture_id=? AND fixture_identity=?
      AND observed_at<=? AND imported_at<=? ORDER BY observed_at,id LIMIT 10001''',
      (snapshot['fixture']['id'],identity,cutoff.isoformat(),cutoff.isoformat())).fetchall()
    if len(query)>10000:
        raise ValueError('More than 10,000 eligible odds observations; this research evaluation limit was exceeded')
    quotes=resolve_quotes([dict(row) for row in query],cutoff,options['max_odds_age_minutes'])
    model_age=(cutoff-datetime.fromisoformat(snapshot['generated_at'])).total_seconds()/3600
    stale_model=model_age>options['max_model_age_hours']
    result=[]
    for market in model_markets(snapshot['models'][model_name]):
        matched=[q for q in quotes if (q['market'],q['selection'],q['line'],q['settlement'])==
            (market['market'],market['selection'],market['line'],market['settlement'])]
        fresh=[q for q in matched if q['status']=='fresh']
        best=max(fresh,key=lambda q:q['decimal_odds']) if fresh else None
        status='model_stale' if stale_model else 'experimental_value' if best else 'stale_odds' if any(q['status']=='stale' for q in matched) else 'conflicting_odds' if matched else 'no_odds'
        calculations=calculate(market['probability'],best['decimal_odds'] if best else None,market['push'])
        if stale_model:
            calculations['expected_value']=None
            calculations['probability_edge']=None
        result.append({**{k:v for k,v in market.items() if k not in ('probability','push')},**calculations,
            'status':status,'best_quote':best,'bookmaker_quotes':sorted(matched,key=lambda q:q['bookmaker_key']),
            'closing_line_value':None,'recommendation':None})
    result.sort(key=lambda row:(row['expected_value'] is None,-(row['expected_value'] or 0),row['market'],row['selection']))
    return {'status':'research_only','created_at':created_at.isoformat(),'pricing_cutoff':cutoff.isoformat(),
            'pricing_time':options['pricing_time'],'model_name':model_name,'model_run_id':snapshot['id'],
            'model_version':snapshot['model_version'],'model_generated_at':snapshot['generated_at'],
            'model_age_hours':model_age,'fixture':snapshot['fixture'],'options':options,'rows':result,
            'odds_observation_ids':[row['id'] for row in query], 'calibration':None,'uncertainty':None,
            'recommendation':None,'warnings':[
                'Theoretical value depends on uncalibrated model probabilities; positive EV is not a betting recommendation.',
                'Manual odds are unverified. A freshness window does not establish that a price is obtainable.',
                'Only regulation-time settlement is supported. Commission, taxes, stake limits and account restrictions are not modelled.',
                'Price comparisons use only observations both observed and received by the pricing cutoff.',
                'Odds movement is from the first stored observation, not a verified market opening price. Closing line value is unavailable.',
                'No correlated selections are combined and no real-money bets are placed.']}
