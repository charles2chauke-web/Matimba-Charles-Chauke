"""Documented CC0 OpenFootball JSON feed. Community data, never official/live odds."""
import csv
import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from backend import history, odds

BASE='https://raw.githubusercontent.com/openfootball/football.json/master'
COMPETITION='English Premier League'
SOURCE='OpenFootball CC0 · community · Europe/London timezone assumed'
LICENSE_URL='https://github.com/openfootball/football.json/blob/master/LICENSE.md'


class FeedError(ValueError):
    pass


def default_seasons(current):
    year=current.year if current.month>=7 else current.year-1
    return [f'{start}-{str(start+1)[-2:]}' for start in range(year-2,year+1)]


def validate_seasons(seasons,current):
    if not 1<=len(seasons)<=3 or len(set(seasons))!=len(seasons):
        raise FeedError('Choose one to three distinct English Premier League seasons')
    for season in seasons:
        if not re.fullmatch(r'20\d\d-\d\d',season):
            raise FeedError('Season must use YYYY-YY format')
        first=int(season[:4])
        if season[-2:]!=str(first+1)[-2:] or not 2010<=first<=int(default_seasons(current)[-1][:4]):
            raise FeedError('Season is outside the supported range')


def download(client,season,cache_dir,current):
    url=f'{BASE}/{season}/en.1.json'
    cache_dir.mkdir(parents=True,exist_ok=True)
    body_path=cache_dir/f'en1-{season}.json'
    meta_path=cache_dir/f'en1-{season}.meta.json'
    cached=metadata=None
    try:
        metadata=json.loads(meta_path.read_text())
        cached=body_path.read_bytes()
        if hashlib.sha256(cached).hexdigest()!=metadata['sha256'] or datetime.fromisoformat(metadata['retrieved_at'])>current:
            cached=metadata=None
    except (OSError,ValueError,KeyError,TypeError):
        cached=metadata=None
    headers={'Accept':'application/json','User-Agent':'Charles-Football-Intelligence/0.1 (CC0 research data)'}
    if metadata and metadata.get('etag'):
        headers['If-None-Match']=metadata['etag']
    failure=None
    try:
        with client.stream('GET',url,headers=headers) as response:
            if response.status_code==304 and cached is not None:
                return {**metadata,'url':url,'payload':json.loads(cached),'cache_status':'revalidated','checked_at':current.isoformat()}
            if response.status_code!=200:
                raise FeedError(f'Public feed returned HTTP {response.status_code} for season {season}')
            chunks=[];size=0
            for chunk in response.iter_bytes():
                size+=len(chunk)
                if size>2_000_000:
                    raise FeedError('Public feed exceeded the 2 MB response limit')
                chunks.append(chunk)
            body=b''.join(chunks)
            payload=json.loads(body)
            if not isinstance(payload,dict):
                raise FeedError('Public feed must contain a JSON object')
            digest=hashlib.sha256(body).hexdigest()
            retrieved=metadata['retrieved_at'] if metadata and digest==metadata['sha256'] else current.isoformat()
            # Keep the last valid cached dataset if a new response has an invalid
            # schema or scores. A response body is not trusted merely for being JSON.
            parse_dataset({'payload':payload,'retrieved_at':retrieved,'url':url},current,14)
            meta={'url':url,'sha256':digest,'retrieved_at':retrieved,'etag':response.headers.get('etag'),
                  'checked_at':current.isoformat(),'license':'CC0-1.0'}
            body_path.write_bytes(body)
            meta_path.write_text(json.dumps(meta))
            return {**meta,'payload':payload,'cache_status':'downloaded'}
    except (httpx.HTTPError,OSError,ValueError) as error:
        failure=str(error) if isinstance(error,FeedError) else 'Public feed network, cache or JSON operation failed'
    if cached is not None:
        return {**metadata,'url':url,'payload':json.loads(cached),'cache_status':'offline_cache',
                'checked_at':current.isoformat(),'warning':failure}
    raise FeedError(failure) from None


def parse_dataset(dataset,current,horizon_days):
    payload=dataset['payload']
    matches=payload.get('matches')
    if not isinstance(payload.get('name'),str) or 'Premier League' not in payload['name'] or not isinstance(matches,list) or len(matches)>1000:
        raise FeedError('Unsupported OpenFootball league schema or match count')
    output=io.StringIO();writer=csv.writer(output)
    writer.writerow(history.REQUIRED+['ht_goals_home','ht_goals_away'])
    fixtures=[];scored_dates=[];missing_time=past_missing=0
    seen=set()
    for index,match in enumerate(matches,1):
        if not isinstance(match,dict):
            raise FeedError(f'Source match {index}: expected object')
        home,away=match.get('team1'),match.get('team2')
        if not all(isinstance(t,str) and 0<len(t.strip())<=150 and not any(ord(c)<32 for c in t) for t in (home,away)) or history.normalized(home)==history.normalized(away):
            raise FeedError(f'Source match {index}: invalid teams')
        if not match.get('time'):
            missing_time+=1
            continue
        try:
            local=datetime.strptime(f"{match['date']} {match['time']}",'%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Europe/London'))
        except (ValueError,KeyError,TypeError):
            raise FeedError(f'Source match {index}: invalid date/time') from None
        kickoff=local.astimezone(timezone.utc)
        fixture={'home':home.strip(),'away':away.strip(),'competition':COMPETITION,'kickoff_utc':kickoff.isoformat()}
        identity=odds.fixture_identity(fixture)
        if identity in seen:
            raise FeedError('Public dataset repeats a fixture identity')
        seen.add(identity)
        score=match.get('score',{})
        # The published files contain both nested {ft:[h,a],ht:[h,a]}
        # and legacy flat [h,a] full-time score representations.
        if isinstance(score,list):
            score={'ft':score}
        elif score is None:
            score={}
        if not isinstance(score,dict):
            raise FeedError(f'Source match {index}: invalid score object')
        full=score.get('ft')
        if full is not None:
            if not isinstance(full,list) or len(full)!=2 or any(type(n) is not int or not 0<=n<=100 for n in full) or kickoff>=current:
                raise FeedError(f'Source match {index}: invalid or future full-time score')
            half=score.get('ht')
            if half is not None and (not isinstance(half,list) or len(half)!=2 or any(type(n) is not int or not 0<=n<=full[i] for i,n in enumerate(half))):
                raise FeedError(f'Source match {index}: invalid half-time score')
            writer.writerow([home,away,COMPETITION,kickoff.astimezone(history.SAST).isoformat(),full[0],full[1],
                             dataset['retrieved_at'],*(half if half is not None else ('',''))])
            scored_dates.append(match['date'])
            fixtures.append({**fixture,'identity':identity,'status':'completed'})
        elif current<kickoff<=current+timedelta(days=horizon_days):
            # JSON does not supply verified status or timezone offsets. Listing
            # is a research candidate, not confirmation that the match will run.
            fixtures.append({**fixture,'identity':identity,'status':'listed'})
        elif kickoff<=current:
            past_missing+=1
    records=history.parse(output.getvalue(),SOURCE,dataset['url'],current) if scored_dates else []
    return {'records':records,'fixtures':fixtures,'latest_result_date':max(scored_dates) if scored_dates else None,
            'missing_times':missing_time,'past_missing_results':past_missing,'matches':len(matches)}


def ingest_history(connection,records,received_at):
    inserted=unchanged=0
    for record in records:
        row=connection.execute('''SELECT o.metrics FROM historical_observations o JOIN historical_matches m ON m.id=o.match_id
            WHERE m.identity=? AND o.source=? AND o.source_url=? ORDER BY o.observed_at DESC,o.id DESC LIMIT 1''',
            (record['identity'],record['source'],record['source_url'])).fetchone()
        if row and json.loads(row['metrics'])==record['metrics']:
            unchanged+=1
            continue
        result=history.ingest(connection,[record],received_at.isoformat())
        inserted+=result['inserted'];unchanged+=result['duplicates']
    return {'inserted':inserted,'unchanged':unchanged}


def ingest_fixtures(connection,fixtures,received_at):
    inserted=updated=preserved=0
    for item in fixtures:
        existing=connection.execute('SELECT id,status,source FROM fixtures WHERE identity=?',(item['identity'],)).fetchone()
        if existing and not existing['source'].startswith('OpenFootball'):
            preserved+=1
            continue
        if existing and existing['status']==item['status']:
            preserved+=1
            continue
        if existing:
            fixture_id=existing['id']
            connection.execute('UPDATE fixtures SET status=?,source=?,imported_at=? WHERE id=?',
                (item['status'],SOURCE,received_at.isoformat(),fixture_id))
            updated+=1
        else:
            cursor=connection.execute('''INSERT INTO fixtures(home,away,competition,kickoff_utc,status,identity,source,imported_at)
                VALUES (?,?,?,?,?,?,?,?)''',(item['home'],item['away'],COMPETITION,item['kickoff_utc'],item['status'],item['identity'],SOURCE,received_at.isoformat()))
            fixture_id=cursor.lastrowid;inserted+=1
        connection.execute('INSERT INTO fixture_observations(fixture_id,identity,status,recorded_at,source) VALUES (?,?,?,?,?)',
            (fixture_id,item['identity'],item['status'],received_at.isoformat(),SOURCE))
    return {'inserted':inserted,'updated':updated,'preserved':preserved}


def fetch(seasons,cache_dir,current,horizon_days,client=None):
    validate_seasons(seasons,current)
    if client is None:
        with httpx.Client(timeout=25,follow_redirects=False) as client:
            return fetch(seasons,cache_dir,current,horizon_days,client)
    result=[]
    for season in seasons:
        data=download(client,season,cache_dir,current)
        parsed=parse_dataset(data,current,horizon_days)
        result.append({'data':{k:v for k,v in data.items() if k!='payload'},**parsed})
    return result
