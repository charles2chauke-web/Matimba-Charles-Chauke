"""API-Sports direct v3 adapter. Reviewed against user-supplied v3.9.3 PDF.

Account status, league/season coverage, fixtures/results and pre-match odds requests are supported.
No odds, xG, statistics or production betting approval is implied.
"""
import hashlib
import json
import threading
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import httpx
from backend import history, odds

BASE = 'https://v3.football.api-sports.io'
SOURCE = 'API-Football · provider-reported'
LOCK = threading.Lock()
_next_request = 0.0
_cooldown_until = 0.0
_daily_remaining = None


class ProviderError(ValueError):
    def __init__(self, code, message, retry_after=None):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after

    def report(self):
        return {'ok': False, 'code': self.code, 'message': str(self), 'retry_after_seconds': self.retry_after}


def initialize(connection):
    connection.executescript('''
    CREATE TABLE IF NOT EXISTS provider_checks (
      provider TEXT PRIMARY KEY, credential_revision TEXT NOT NULL,
      checked_at TEXT NOT NULL, result TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS api_football_teams (
      provider_id INTEGER PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS api_football_leagues (
      provider_id INTEGER PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS api_football_fixtures (
      provider_id INTEGER PRIMARY KEY, fixture_id INTEGER NOT NULL UNIQUE,
      home_id INTEGER NOT NULL, away_id INTEGER NOT NULL, league_id INTEGER NOT NULL,
      season INTEGER NOT NULL, last_received_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS api_football_observations (
      id INTEGER PRIMARY KEY, provider_id INTEGER NOT NULL, received_at TEXT NOT NULL,
      provider_status TEXT NOT NULL, kickoff_utc TEXT NOT NULL,
      home_id INTEGER NOT NULL, away_id INTEGER NOT NULL, league_id INTEGER NOT NULL,
      season INTEGER NOT NULL, metrics TEXT);
    ''')


def revision(key):
    return hashlib.sha256(key.encode()).hexdigest()


def integer(value, minimum=0, maximum=1_000_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProviderError('invalid_response', 'Provider returned an invalid numeric field. No data was imported.')
    return value


def text(value, maximum=150):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= maximum or any(ord(c) < 32 for c in value):
        raise ProviderError('invalid_response', 'Provider returned an invalid text field. No data was imported.')
    return value.strip()


def request(client, key, endpoint, params=None):
    """Fixed host/paths, no redirects or retries; raw provider errors never escape."""
    global _next_request, _cooldown_until, _daily_remaining
    if endpoint not in ('/status', '/leagues', '/fixtures', '/odds'):
        raise ValueError('Unsupported provider endpoint')
    if not key or not key.isascii() or any(ord(c)<33 or ord(c)>126 for c in key):
        raise ProviderError('invalid_key_format', 'The saved key has invalid characters. Copy the direct API-Sports key again without spaces.')
    if endpoint != '/status' and _daily_remaining == 0:
        raise ProviderError('quota_exhausted', 'API-Football reports no daily requests remaining. Wait for your provider quota to reset.')
    cooldown = _cooldown_until - time.monotonic()
    if cooldown > 0:
        raise ProviderError('rate_limited', 'Provider requests are paused. Wait before trying again.', int(cooldown) + 1)
    remaining = _next_request - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    _next_request = time.monotonic() + 1
    try:
        with client.stream('GET', BASE + endpoint, params=params, headers={'x-apisports-key': key}) as response:
            limits = {}
            for name, label in [('x-ratelimit-requests-limit', 'daily_limit'), ('x-ratelimit-requests-remaining', 'daily_remaining'),
                                ('x-ratelimit-limit', 'minute_limit'), ('x-ratelimit-remaining', 'minute_remaining')]:
                raw = response.headers.get(name, '')
                limits[label] = int(raw) if raw.isdigit() and len(raw) < 10 else None
            retry = response.headers.get('retry-after', '')
            wait = min(int(retry), 86400) if retry.isdigit() and len(retry) < 10 else 60
            if retry and not retry.isdigit():
                try:
                    wait = max(1,min(86400,int((parsedate_to_datetime(retry)-datetime.now(timezone.utc)).total_seconds())))
                except (ValueError,TypeError,OverflowError):
                    pass
            if endpoint != '/status' and _daily_remaining is not None:
                _daily_remaining = max(0,_daily_remaining-1)
            if limits['daily_remaining'] is not None:
                _daily_remaining = limits['daily_remaining']
            if response.status_code == 429:
                _cooldown_until = time.monotonic() + wait
                raise ProviderError('rate_limited', 'API-Football rate limit reached. Wait before trying again.', wait)
            if response.status_code in (401, 403):
                raise ProviderError('access_denied', 'API-Football access was denied. Check the direct API-Sports key, subscription and any account IP restrictions; a network proxy can also deny access.')
            if response.status_code != 200:
                raise ProviderError('provider_unavailable', 'API-Football returned HTTP ' + str(response.status_code) + '. Try again later; the saved key has not been removed.')
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 4_000_000:
                    raise ProviderError('invalid_response', 'Provider response exceeded the 4 MB safety limit. No data was imported.')
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get('get') != endpoint[1:] or 'errors' not in payload:
                raise ProviderError('invalid_response', 'Provider response did not match the documented API envelope.')
            errors = payload['errors']
            if not isinstance(errors, (dict, list)):
                raise ProviderError('invalid_response', 'Provider returned an invalid error envelope.')
            if errors:
                # Inspect only for classification; never return upstream messages or request headers.
                categories = ' '.join(str(k).lower() for k in errors) if isinstance(errors, dict) else ''
                if any(word in categories for word in ('rate', 'requests', 'limit')):
                    _cooldown_until = time.monotonic() + wait
                    raise ProviderError('quota_or_rate_limit', 'API-Football rejected the request because of a quota or rate limit. Check the provider dashboard before retrying.', wait)
                if any(word in categories for word in ('token', 'key', 'auth', 'access')):
                    raise ProviderError('authentication_failed', 'API-Football rejected authentication or account access. Check your direct API-Sports key and account restrictions.')
                raise ProviderError('provider_rejected', 'API-Football reported an API error. Check subscription coverage and the provider dashboard. No data was imported.')
            if limits.get('minute_remaining') == 0:
                _cooldown_until = time.monotonic() + 60
            elif limits.get('minute_limit'):
                _next_request = max(_next_request, time.monotonic() + 60 / limits['minute_limit'])
            return payload, limits
    except ProviderError:
        raise
    except httpx.TimeoutException:
        raise ProviderError('timeout', 'API-Football timed out. Check your Internet connection and try again later.') from None
    except httpx.HTTPError:
        raise ProviderError('network_error', 'Could not securely reach API-Football. Check your Internet connection, proxy or firewall. Your key has not been declared invalid.') from None
    except (ValueError, TypeError):
        raise ProviderError('invalid_response', 'API-Football returned unreadable JSON. No data was imported.') from None


def status(client, key):
    global _daily_remaining
    payload, limits = request(client, key, '/status')
    try:
        data = payload['response']
        subscription, usage = data['subscription'], data['requests']
        active = subscription['active']
        if type(active) is not bool:
            raise TypeError()
        plan = text(subscription['plan'], 80)
        end = datetime.fromisoformat(subscription['end'])
        if end.tzinfo is None:
            raise ValueError()
        current, limit = integer(usage['current']), integer(usage['limit_day'], 1)
        if key in plan:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ProviderError('invalid_response', 'API-Football account response is missing valid subscription or quota fields.') from None
    # Deliberately discard account name/email and all unrecognised fields.
    usable = active and end > datetime.now(timezone.utc)
    _daily_remaining = limits.get('daily_remaining') if limits.get('daily_remaining') is not None else max(0,limit-current)
    return {'ok': usable, 'code': 'connected' if usable else 'subscription_inactive',
            'message': 'Connection successful. Account access verified; league coverage is checked during import.' if usable else 'Key accepted, but the subscription is inactive or expired.',
            'subscription': {'plan': plan, 'active': active, 'end': end.isoformat()},
            'usage': {'current': current, 'daily_limit': limit, 'daily_remaining': max(0, limit-current), **{k: v for k, v in limits.items() if v is not None}}}


def collection(client, key, endpoint, params):
    payload, limits = request(client, key, endpoint, params)
    rows, paging = payload.get('response'), payload.get('paging')
    if not isinstance(rows, list) or len(rows) > 1500 or type(payload.get('results')) is not int or payload['results'] != len(rows):
        raise ProviderError('invalid_response', 'Provider returned an incomplete or oversized collection. No data was imported.')
    # The documented league/fixtures queries have no page parameter. Refuse
    # incomplete collections instead of inventing pagination or silently truncating.
    if not isinstance(paging, dict) or paging.get('current') != 1 or paging.get('total') != 1:
        raise ProviderError('incomplete_response', 'This response requires unsupported pagination. No partial dataset was imported.')
    return rows, limits


STATUS = {'NS': 'scheduled', 'TBD': 'listed', 'PST': 'postponed', 'CANC': 'cancelled',
          'FT': 'completed', 'AET': 'completed', 'PEN': 'completed', 'ABD': 'abandoned',
          'AWD': 'awarded', 'WO': 'walkover',
          **{s: 'live' for s in ('1H', 'HT', '2H', 'ET', 'BT', 'P', 'SUSP', 'INT', 'LIVE')}}


def parse_fixture(raw, league_id, season, current):
    try:
        fixture, league, teams = raw['fixture'], raw['league'], raw['teams']
        provider_id = integer(fixture['id'], 1)
        if integer(league['id'], 1) != league_id or integer(league['season'], 1900, 2200) != season:
            raise ValueError()
        home, away = teams['home'], teams['away']
        home_id, away_id = integer(home['id'], 1), integer(away['id'], 1)
        if home_id == away_id:
            raise ValueError()
        code = fixture['status']['short']
        if code not in STATUS:
            raise ValueError()
        # TBD/null dates are not actionable schedule assertions.
        if fixture.get('date') is None and code == 'TBD':
            return None
        kickoff = datetime.fromisoformat(fixture['date'])
        if kickoff.tzinfo is None:
            raise ValueError()
        kickoff = kickoff.astimezone(timezone.utc)
        metrics = None
        if code == 'FT':
            if kickoff >= current:
                raise ValueError()
            full = raw['score']['fulltime']
            if full['home'] is not None and full['away'] is not None:
                metrics = {field: None for field in history.METRICS}
                half_score = raw['score'].get('halftime')
                if half_score is None:
                    half_score = {}
                if not isinstance(half_score,dict):
                    raise ValueError()
                for side in ('home', 'away'):
                    metrics['goals_' + side] = integer(full[side], 0, 100)
                    half = half_score.get(side)
                    metrics['ht_goals_' + side] = integer(half, 0, full[side]) if half is not None else None
        return {'provider_id': provider_id, 'home_id': home_id, 'away_id': away_id,
                'home': text(home['name']), 'away': text(away['name']),
                'league_id': league_id, 'season': season, 'kickoff_utc': kickoff.isoformat(),
                'status': STATUS[code], 'provider_status': code, 'metrics': metrics}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProviderError('invalid_response', 'Provider fixture schema, status, date or regulation score is invalid. No data was imported.') from None


def fetch(client, key, league_id, seasons, current):
    datasets = []
    seen = set()
    for season in seasons:
        leagues, _ = collection(client, key, '/leagues', {'id': league_id, 'season': season})
        if len(leagues) != 1:
            raise ProviderError('coverage_unavailable', 'The requested league/season is unavailable to this account. Choose a covered season.')
        try:
            league = leagues[0]
            if integer(league['league']['id'], 1) != league_id:
                raise ValueError()
            name = text(league['league']['name'])
            country = text(league['country']['name'], 80)
            entries = [s for s in league['seasons'] if s['year'] == season]
            if len(entries) != 1:
                raise ValueError()
            coverage = entries[0].get('coverage', {})
            # Booleans only, never pass raw upstream objects to the browser/database.
            coverage = {k: v for k, v in coverage.items() if k in ('odds', 'injuries', 'predictions', 'standings') and type(v) is bool}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError('invalid_response', 'Provider league/season response was invalid. No data was imported.') from None
        rows, usage = collection(client, key, '/fixtures', {'league': league_id, 'season': season})
        parsed = []
        for raw in rows:
            item = parse_fixture(raw, league_id, season, current)
            if item is None:
                continue
            if item['provider_id'] in seen:
                raise ProviderError('invalid_response', 'Provider repeated a fixture ID. No data was imported.')
            seen.add(item['provider_id'])
            parsed.append(item)
        datasets.append({'season': season, 'league_id': league_id, 'name': name, 'country': country,
                         'coverage': coverage, 'fixtures': parsed, 'usage': usage})
    return datasets


def daily_leagues(client,key,current):
    """Discover future NS fixtures on the generation day in South African time."""
    day=current.astimezone(history.SAST).date()
    rows,_=collection(client,key,'/fixtures',{'date':day.isoformat(),'timezone':'Africa/Johannesburg'})
    leagues={};seen=set()
    try:
        for raw in rows:
            if raw['fixture']['status']['short']!='NS':continue
            league_id=integer(raw['league']['id'],1)
            season=integer(raw['league']['season'],1900,2200)
            fixture=parse_fixture(raw,league_id,season,current)
            if fixture is None:continue
            kickoff=datetime.fromisoformat(fixture['kickoff_utc'])
            if kickoff<=current or kickoff.astimezone(history.SAST).date()!=day:continue
            pid=fixture['provider_id']
            if pid in seen:raise ValueError()
            seen.add(pid)
            if league_id in leagues and leagues[league_id]['season']!=season:raise ValueError()
            item=leagues.setdefault(league_id,{'league_id':league_id,'season':season,'fixture_ids':[],'first_kickoff':fixture['kickoff_utc']})
            item['fixture_ids'].append(pid)
            item['first_kickoff']=min(item['first_kickoff'],fixture['kickoff_utc'])
    except (KeyError,TypeError,ValueError):
        raise ProviderError('invalid_daily_fixtures','Daily fixture response is invalid. No Top Five build was started.') from None
    return sorted(leagues.values(),key=lambda l:(l['first_kickoff'],l['league_id']))


def available_seasons(client,key,league_id,season):
    rows,_=collection(client,key,'/leagues',{'id':league_id})
    try:
        if len(rows)!=1 or rows[0]['league']['id']!=league_id:raise ValueError()
        years={integer(s['year'],1900,2200) for s in rows[0]['seasons']}
        if season not in years:raise ValueError()
        return sorted(y for y in years if season-2<=y<=season)
    except (KeyError,TypeError,ValueError):
        raise ProviderError('coverage_unavailable','The daily fixture league/season is not available in this account’s league metadata.') from None


def canonical(connection, table, provider_id, name):
    existing = connection.execute('SELECT canonical_name FROM ' + table + ' WHERE provider_id=?', (provider_id,)).fetchone()
    if existing:
        return existing['canonical_name']
    name = name[:130]
    collision = any(history.normalized(row[0])==history.normalized(name)
                    for row in connection.execute('SELECT canonical_name FROM ' + table))
    canonical_name = name if not collision else f'{name} ({provider_id})'
    connection.execute('INSERT INTO ' + table + ' VALUES (?,?)', (provider_id, canonical_name))
    return canonical_name


def record_history(connection, fixture, metrics, received):
    identity = fixture['identity']
    prior = connection.execute('''SELECT o.metrics FROM historical_observations o JOIN historical_matches m ON m.id=o.match_id
        WHERE m.identity=? AND o.source=? ORDER BY o.observed_at DESC,o.id DESC LIMIT 1''', (identity, SOURCE)).fetchone()
    absent = metrics is None
    metrics = metrics if metrics is not None else {field: None for field in history.METRICS}
    if (prior is None and absent) or (prior is not None and json.loads(prior['metrics']) == metrics):
        return 0
    digest = hashlib.sha256(json.dumps([identity, SOURCE, received.isoformat(), metrics], sort_keys=True).encode()).hexdigest()
    row = {'home': fixture['home'], 'away': fixture['away'], 'competition': fixture['competition'],
           'kickoff': fixture['kickoff_utc'], 'identity': identity, 'source': SOURCE,
           'source_url': BASE + '/fixtures', 'observed_at': received.isoformat(), 'metrics': metrics, 'digest': digest}
    return history.ingest(connection, [row], received.isoformat())['inserted']


def ingest(connection, datasets, received):
    inserted = updated = results = unchanged = 0
    competitions = []
    latest = None
    for dataset in datasets:
        league_id = dataset['league_id']
        competition = canonical(connection, 'api_football_leagues', league_id, 'API-Football · ' + dataset['name'] + ' · ' + dataset['country'])
        competitions.append(competition)
        for item in dataset['fixtures']:
            home = canonical(connection, 'api_football_teams', item['home_id'], item['home'])
            away = canonical(connection, 'api_football_teams', item['away_id'], item['away'])
            fixture = {**item, 'home': home, 'away': away, 'competition': competition}
            fixture['identity'] = odds.fixture_identity(fixture)
            link = connection.execute('SELECT * FROM api_football_fixtures WHERE provider_id=?', (item['provider_id'],)).fetchone()
            existing = connection.execute('SELECT * FROM fixtures WHERE id=?', (link['fixture_id'],)).fetchone() if link else None
            if link and (link['home_id'], link['away_id'], link['league_id']) != (item['home_id'], item['away_id'], league_id):
                raise ProviderError('identity_conflict', 'A stable provider fixture ID changed teams or competition. Import was rolled back for review.')
            if existing and existing['identity'] != fixture['identity']:
                # Invalidate old-schedule history by a new immutable observation;
                # old prediction/odds snapshots stay attached to their old identity.
                record_history(connection, dict(existing), None, received)
            conflict = connection.execute('SELECT id FROM fixtures WHERE identity=?',(fixture['identity'],)).fetchone()
            if conflict and (existing is None or conflict['id']!=existing['id']):
                raise ProviderError('identity_conflict', 'Two fixture IDs resolve to the same schedule and teams. Import was rolled back for review.')
            if existing is None:
                cursor = connection.execute('''INSERT INTO fixtures(home,away,competition,kickoff_utc,status,identity,source,imported_at)
                    VALUES (?,?,?,?,?,?,?,?)''', (home, away, competition, item['kickoff_utc'], item['status'], fixture['identity'], SOURCE, received.isoformat()))
                fixture_id = cursor.lastrowid
                connection.execute('INSERT INTO api_football_fixtures VALUES (?,?,?,?,?,?,?)',
                                   (item['provider_id'], fixture_id, item['home_id'], item['away_id'], league_id, item['season'], received.isoformat()))
                inserted += 1
                changed = True
            else:
                fixture_id = existing['id']
                changed = (existing['identity'], existing['status']) != (fixture['identity'], item['status'])
                connection.execute('UPDATE fixtures SET home=?,away=?,competition=?,kickoff_utc=?,status=?,identity=?,source=?,imported_at=? WHERE id=?',
                                   (home, away, competition, item['kickoff_utc'], item['status'], fixture['identity'], SOURCE, received.isoformat(), fixture_id))
                connection.execute('UPDATE api_football_fixtures SET last_received_at=?,season=? WHERE provider_id=?', (received.isoformat(), item['season'], item['provider_id']))
                updated += int(changed)
                unchanged += int(not changed)
            # Record each refreshed status so schedule freshness is auditable.
            connection.execute('INSERT INTO fixture_observations(fixture_id,identity,status,recorded_at,source) VALUES (?,?,?,?,?)',
                               (fixture_id, fixture['identity'], item['status'], received.isoformat(), SOURCE))
            connection.execute('''INSERT INTO api_football_observations(provider_id,received_at,provider_status,kickoff_utc,
                home_id,away_id,league_id,season,metrics) VALUES (?,?,?,?,?,?,?,?,?)''',
                (item['provider_id'],received.isoformat(),item['provider_status'],item['kickoff_utc'],item['home_id'],item['away_id'],
                 league_id,item['season'],json.dumps(item['metrics'],allow_nan=False)))
            results += record_history(connection, fixture, item['metrics'], received)
            if item['metrics'] is not None:
                latest = max(latest or item['kickoff_utc'], item['kickoff_utc'])
        returned = {item['provider_id'] for item in dataset['fixtures']}
        previous = connection.execute('''SELECT f.*,p.provider_id FROM fixtures f JOIN api_football_fixtures p ON p.fixture_id=f.id
            WHERE p.league_id=? AND p.season=?''',(league_id,dataset['season'])).fetchall()
        for old in previous:
            if old['provider_id'] not in returned and old['status']!='unavailable':
                # Absence is not a fabricated cancellation. Withhold a previously
                # observed fixture/result until the provider returns it again.
                connection.execute("UPDATE fixtures SET status='unavailable',imported_at=? WHERE id=?",(received.isoformat(),old['id']))
                connection.execute('INSERT INTO fixture_observations(fixture_id,identity,status,recorded_at,source) VALUES (?,?,?,?,?)',
                                   (old['id'],old['identity'],'unavailable',received.isoformat(),SOURCE))
                record_history(connection,dict(old),None,received)
    return {'fixtures_inserted': inserted, 'fixtures_updated': updated, 'fixtures_unchanged': unchanged,
            'history_inserted': results, 'competitions': sorted(set(competitions)), 'latest_result_utc': latest}


def client():
    return httpx.Client(timeout=httpx.Timeout(25, connect=10), follow_redirects=False)
