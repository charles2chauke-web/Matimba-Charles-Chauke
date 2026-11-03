"""Bounded API-Football pre-match odds. Never relabel retrieval as quote update."""
import csv
from collections import Counter
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
        except (KeyError,TypeError,ValueError,OverflowError):raise provider.ProviderError('invalid_odds','A bookmaker price is missing or invalid.') from None
        if not math.isfinite(price) or not 1<price<=10000:
            raise provider.ProviderError('invalid_odds','A bookmaker price is outside the supported range.')
        result.append((*mapped,price))
    return result


def reject(reason):
    # Reason names are fixed by this adapter. Never expose upstream values.
    raise provider.ProviderError('odds_'+reason, 'Odds rejected: '+reason.replace('_',' ')+'.')


def metadata(entry,fixtures,league,season,current):
    if not isinstance(entry,dict):reject('fixture_record_not_object')
    source=entry.get('fixture')
    if not isinstance(source,dict):reject('fixture_metadata_missing')
    try:pid=provider.integer(source.get('id'),1)
    except provider.ProviderError:reject('fixture_id_invalid')
    competition=entry.get('league')
    if not isinstance(competition,dict):reject('league_metadata_missing')
    if competition.get('id')!=league:reject('league_id_mismatch')
    if competition.get('season')!=season:reject('league_season_mismatch')
    fixture=fixtures.get(pid)
    if fixture is None:return pid,None
    if fixture['status']!='scheduled' or datetime.fromisoformat(fixture['kickoff_utc'])<=current:return pid,None
    def timestamp(value,field):
        if not isinstance(value,str):reject(field+'_missing')
        try:result=datetime.fromisoformat(value)
        except ValueError:reject(field+'_invalid')
        if result.tzinfo is None:reject(field+'_timezone_missing')
        return result
    date=timestamp(source.get('date'),'fixture_date')
    update=timestamp(entry.get('update'),'quote_update')
    kickoff=datetime.fromisoformat(fixture['kickoff_utc'])
    if date!=kickoff:reject('fixture_kickoff_mismatch')
    if update>current:reject('quote_update_in_future')
    if update>=kickoff:reject('quote_not_prematch')
    return pid,(fixture,update)


def parse(rows,fixtures,league,season,current):
    """Strict parser; invalid fixtures or quotes abort this particular batch."""
    normalized=[];skipped=0;seen=set()
    try:
        for entry in rows:
            pid,context=metadata(entry,fixtures,league,season,current)
            if pid in seen:reject('duplicate_fixture_id')
            seen.add(pid)
            if context is None:
                skipped+=1;continue
            fixture,update=context
            bookmakers=entry.get('bookmakers')
            if not isinstance(bookmakers,list) or len(bookmakers)>100:reject('bookmakers_invalid')
            for book in bookmakers:
                bid=provider.integer(book['id'],1)
                name=provider.text(book['name'],70)
                bookmaker=f'{name} · API {bid}'
                bets=book['bets']
                if not isinstance(bets,list) or len(bets)>200:reject('markets_invalid')
                for bet in bets:
                    for name,selection,line,price in market(bet):
                        normalized.append([fixture['id'],bookmaker,name,selection,line,price,update.astimezone(timezone.utc).isoformat(),odds.SETTLEMENT])
                        if len(normalized)>50000:reject('quote_limit_exceeded')
    except provider.ProviderError:
        raise
    except (KeyError,TypeError,ValueError,AttributeError):
        reject('bookmaker_or_market_schema_invalid')
    records=[]
    for start in range(0,len(normalized),1000):
        output=io.StringIO();writer=csv.writer(output);writer.writerow(odds.HEADER)
        writer.writerows(normalized[start:start+1000])
        records.extend(odds.parse(output.getvalue(),SOURCE,provider.BASE+'/odds',current,{f['id']:f for f in fixtures.values()}))
    return records,skipped


def parse_with_diagnostics(rows,fixtures,league,season,current):
    """Quarantine at the smallest safe boundary, then validate before DB writes.

    A bad fixture identity/time excludes the whole fixture. A malformed book or
    market excludes that object. An unusable price excludes just that price.
    Invalid pagination remains a fatal error in pages(); it is never salvaged.
    """
    reasons=Counter();samples=[];records=[];skipped=0
    ids=[]
    for row in rows:
        try:ids.append(provider.integer(row['fixture']['id'],1))
        except (provider.ProviderError,KeyError,TypeError):ids.append(None)
    duplicates={pid for pid,count in Counter(ids).items() if pid is not None and count>1}
    def note(reason,pid=None):
        reasons[reason]+=1
        if len(samples)<12 and not any(s['reason']==reason for s in samples):
            sample={'reason':reason,'provider_fixture_id':pid}
            # Whitelist parsed times and primitive type names, never raw strings.
            source=row if isinstance(row,dict) else {}
            fixture=source.get('fixture') if isinstance(source.get('fixture'),dict) else {}
            competition=source.get('league') if isinstance(source.get('league'),dict) else {}
            sample['field_types']={k:type(v).__name__ for k,v in {
                'fixture_date':fixture.get('date'),'quote_update':source.get('update'),
                'league_id':competition.get('id'),'season':competition.get('season')}.items()}
            dates={}
            for field,value in [('fixture_date',fixture.get('date')),('quote_update',source.get('update'))]:
                if isinstance(value,str):
                    try:
                        parsed=datetime.fromisoformat(value)
                        if parsed.tzinfo is not None:dates[field]=parsed.astimezone(timezone.utc).isoformat()
                    except (ValueError,OverflowError):pass
            if pid in fixtures:dates['expected_kickoff']=fixtures[pid]['kickoff_utc']
            sample['parsed_times']=dates
            samples.append(sample)
    def value_rejection(value):
        if not isinstance(value,dict):return 'quote_value_not_object'
        if not isinstance(value.get('value'),str):return 'quote_selection_invalid'
        if value.get('odd') is None or value.get('odd')=='':return 'quote_price_missing'
        try:price=float(value['odd'])
        except (TypeError,ValueError,OverflowError):return 'quote_price_not_numeric'
        if not math.isfinite(price):return 'quote_price_not_finite'
        if price<=1:return 'quote_price_not_above_one'
        if price>10000:return 'quote_price_above_limit'
        return 'quote_value_invalid'
    supported={1:'Match Winner',5:'Goals Over/Under',8:'Both Teams Score',12:'Double Chance'}
    for row,pid in zip(rows,ids):
        try:
            if pid in duplicates:reject('duplicate_fixture_id')
            _,context=metadata(row,fixtures,league,season,current)
            if context is None:
                note('fixture_unknown_started_or_not_scheduled',pid);skipped+=1;continue
            books=row.get('bookmakers')
            if not isinstance(books,list) or len(books)>100:reject('bookmakers_invalid')
            cleaned=[]
            # Ambiguous duplicate bookmaker IDs exclude all copies, not first wins.
            book_ids=[b.get('id') for b in books if isinstance(b,dict) and type(b.get('id')) is int]
            duplicate_books={bid for bid,count in Counter(book_ids).items() if count>1}
            for book in books:
                try:
                    if not isinstance(book,dict):reject('bookmaker_invalid')
                    bid=provider.integer(book.get('id'),1)
                    if bid in duplicate_books:reject('duplicate_bookmaker_id')
                    provider.text(book.get('name'),70)
                    bets=book.get('bets')
                    if not isinstance(bets,list) or len(bets)>200:reject('markets_invalid')
                except provider.ProviderError as error:
                    note(error.code,pid);continue
                clean_bets=[]
                for bet in bets:
                    if not isinstance(bet,dict) or type(bet.get('id')) is not int:continue
                    if bet['id'] not in supported:continue
                    if supported[bet['id']]!=bet.get('name'):
                        note('supported_market_name_mismatch',pid);continue
                    values=bet.get('values')
                    if not isinstance(values,list) or len(values)>50:
                        note('market_values_invalid',pid);continue
                    valid=[]
                    for value in values:
                        try:
                            # Reuse exact market/selection whitelist and strict prices.
                            parsed=market({**bet,'values':[value]})
                            if parsed:valid.append(value)
                            else:note('unsupported_selection_or_line',pid)
                        except (provider.ProviderError,TypeError,ValueError,OverflowError):
                            note(value_rejection(value),pid)
                    if valid:clean_bets.append({**bet,'values':valid})
                if clean_bets:cleaned.append({**book,'bets':clean_bets})
            safe={**row,'bookmakers':cleaned}
            accepted,_=parse([safe],fixtures,league,season,current)
            if not accepted:note('no_supported_quotes_returned',pid)
            records.extend(accepted)
            if len(records)>50000:reject('quote_limit_exceeded')
        except provider.ProviderError as error:
            if error.code=='odds_quote_limit_exceeded':raise
            note(error.code,pid);skipped+=1
        except (KeyError,TypeError,ValueError,AttributeError):
            note('fixture_schema_invalid',pid);skipped+=1
    diagnostic={'fixtures_returned':len(rows),'quotes_accepted':len(records),'fixtures_skipped':skipped,
                'rejection_counts':dict(reasons),'samples':samples}
    return records,diagnostic
