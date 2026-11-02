"""Bounded API-Football pre-match odds. Never relabel retrieval as quote update."""
import csv
import io
from datetime import datetime, timezone
import math
from backend import api_football as provider, odds

SOURCE='API-Football · pre-match bookmaker quotes'


def pages(client,key,league,season,max_pages=5,day=None):
    rows=[]
    total=None
    for page in range(1,max_pages+1):
        params={'league':league,'season':season,'page':page}
        if day is not None:params.update({'date':day,'timezone':'Africa/Johannesburg'})
        payload,_=provider.request(client,key,'/odds',params)
        batch,paging=payload.get('response'),payload.get('paging')
        if not isinstance(batch,list) or len(batch)>10 or type(payload.get('results')) is not int or payload['results']!=len(batch) or not isinstance(paging,dict):
            raise provider.ProviderError('invalid_odds','Provider odds collection is malformed. No odds from this league were imported.')
        count=paging.get('total')
        if page==1 and count==0 and not batch and paging.get('current')==1:return []
        if type(count) is not int or not 1<=count<=max_pages or paging.get('current')!=page or (total is not None and count!=total):
            raise provider.ProviderError('incomplete_odds','Odds pagination changed or exceeds the five-page limit. No partial league odds were imported.')
        total=count
        rows.extend(batch)
        if page==total:return rows
    raise provider.ProviderError('incomplete_odds','Odds pagination exceeded its bound.')


def market(bet):
    """Whitelist full-match markets by BOTH provider ID and name; skip others."""
    names={1:'Match Winner',5:'Goals Over/Under',8:'Both Teams Score',12:'Double Chance'}
    if not isinstance(bet,dict) or names.get(bet.get('id'))!=bet.get('name'):
        return []
    values=bet.get('values')
    if not isinstance(values,list) or len(values)>50:
        raise provider.ProviderError('invalid_odds','A supported odds market has invalid values.')
    result=[]
    for value in values:
        if not isinstance(value,dict):raise provider.ProviderError('invalid_odds','A bookmaker quote is invalid.')
        label=value.get('value')
        if bet['id']==1:
            selection={'Home':'home','Draw':'draw','Away':'away'}.get(label)
            mapped=('1x2',selection,'') if selection else None
        elif bet['id']==8:
            selection={'Yes':'yes','No':'no'}.get(label)
            mapped=('btts',selection,'') if selection else None
        elif bet['id']==12:
            selection={'Home/Draw':'home_or_draw','Draw/Away':'away_or_draw','Home/Away':'home_or_away'}.get(label)
            mapped=('double_chance',selection,'') if selection else None
        else:
            labels={f'{direction} {line}':('goals',direction.lower(),str(line)) for direction in ('Over','Under') for line in (.5,1.5,2.5,3.5)}
            mapped=labels.get(label)
        if mapped is None:continue
        try:price=float(value['odd'])
        except (KeyError,TypeError,ValueError):raise provider.ProviderError('invalid_odds','A bookmaker price is missing or invalid.') from None
        if not math.isfinite(price) or not 1<price<=10000:
            raise provider.ProviderError('invalid_odds','A bookmaker price is outside the supported range.')
        result.append((*mapped,price))
    return result


def parse(rows,fixtures,league,season,current):
    """Fixture map is keyed by stable provider ID and includes the local identity."""
    normalized=[]
    skipped=0;seen=set();written=0
    try:
        for entry in rows:
            pid=provider.integer(entry['fixture']['id'],1)
            if pid in seen:raise ValueError()
            seen.add(pid)
            if entry['league']['id']!=league or entry['league']['season']!=season:raise ValueError()
            fixture=fixtures.get(pid)
            if fixture is None or fixture['status']!='scheduled' or datetime.fromisoformat(fixture['kickoff_utc'])<=current:
                skipped+=1;continue
            date=datetime.fromisoformat(entry['fixture']['date'])
            update=datetime.fromisoformat(entry['update'])
            kickoff=datetime.fromisoformat(fixture['kickoff_utc'])
            if date.tzinfo is None or update.tzinfo is None or date!=kickoff or update>current or update>=kickoff:
                raise ValueError()
            bookmakers=entry['bookmakers']
            if not isinstance(bookmakers,list) or len(bookmakers)>100:raise ValueError()
            for book in bookmakers:
                bid=provider.integer(book['id'],1)
                name=provider.text(book['name'],70)
                # Provider bookmaker ID prevents identically named bookmakers merging.
                bookmaker=f'{name} · API {bid}'
                bets=book['bets']
                if not isinstance(bets,list) or len(bets)>200:raise ValueError()
                for bet in bets:
                    for name,selection,line,price in market(bet):
                        normalized.append([fixture['id'],bookmaker,name,selection,line,price,update.astimezone(timezone.utc).isoformat(),odds.SETTLEMENT])
                        written+=1
                        if written>50000:raise ValueError()
    except (KeyError,TypeError,ValueError,AttributeError):
        raise provider.ProviderError('invalid_odds','Odds identity, timestamps, schema or prices are invalid. No odds from this league were imported.') from None
    records=[]
    for start in range(0,len(normalized),1000):
        output=io.StringIO();writer=csv.writer(output);writer.writerow(odds.HEADER)
        writer.writerows(normalized[start:start+1000])
        records.extend(odds.parse(output.getvalue(),SOURCE,provider.BASE+'/odds',current,{f['id']:f for f in fixtures.values()}))
    return records,skipped
