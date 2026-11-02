"""Validated, immutable historical observations; never infer missing statistics."""
import csv
import hashlib
import io
import json
import math
import unicodedata
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit

SAST = timezone(timedelta(hours=2))
REQUIRED = ['home', 'away', 'competition', 'kickoff_sast', 'goals_home', 'goals_away', 'observed_at']
OPTIONAL = ['xg_home', 'xg_away', 'corners_home', 'corners_away', 'yellow_home',
            'yellow_away', 'red_home', 'red_away', 'ht_goals_home', 'ht_goals_away']
METRICS = ['goals_home', 'goals_away'] + OPTIONAL


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def initialize(connection):
    connection.executescript('''
    CREATE TABLE IF NOT EXISTS historical_matches (
      id INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE,
      home TEXT NOT NULL, away TEXT NOT NULL, competition TEXT NOT NULL,
      home_key TEXT NOT NULL, away_key TEXT NOT NULL, competition_key TEXT NOT NULL,
      kickoff_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS historical_observations (
      id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL REFERENCES historical_matches(id),
      source TEXT NOT NULL, source_url TEXT NOT NULL, observed_at TEXT NOT NULL,
      imported_at TEXT NOT NULL, metrics TEXT NOT NULL, digest TEXT NOT NULL UNIQUE);
    CREATE INDEX IF NOT EXISTS history_match_observed ON historical_observations(match_id,observed_at);
    ''')


def source_metadata(source, source_url):
    source = source.strip()
    if not source or len(source) > 120 or any(ord(c) < 32 for c in source):
        raise ValueError('Source name must contain 1–120 printable characters')
    source_url = source_url.strip()
    if source_url:
        parsed = urlsplit(source_url)
        if (len(source_url) > 500 or parsed.scheme != 'https' or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Source URL must be HTTPS with no credentials, query string or fragment')
    return source, source_url


def utc_datetime(text, field, current):
    try:
        value = datetime.fromisoformat(text)
        if field == 'kickoff_sast':
            if value.tzinfo is None:
                value = value.replace(tzinfo=SAST)
            if value.utcoffset() != timedelta(hours=2):
                raise ValueError()
        elif value.tzinfo is None:
            raise ValueError()
        value = value.astimezone(timezone.utc)
        if value > current:
            raise ValueError()
        return value
    except ValueError:
        raise ValueError(f'{field} must be a past ISO datetime with an offset; kickoff must be SAST') from None


def parse(content, source, source_url, current):
    source, source_url = source_metadata(source, source_url)
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
    fields = reader.fieldnames or []
    if (not set(REQUIRED).issubset(fields) or set(fields) - set(REQUIRED + OPTIONAL)
            or len(fields) != len(set(fields))):
        raise ValueError('Required historical CSV columns: ' + ','.join(REQUIRED) + '. Optional: ' + ','.join(OPTIONAL))
    records = []
    for index, row in enumerate(reader, 2):
        try:
            if index > 5001:
                raise ValueError('Maximum 5,000 rows per import')
            if None in row or any(value is None for value in row.values()):
                raise ValueError('Column count does not match the header')
            row = {key: value.strip() for key, value in row.items()}
            for field in ('home', 'away', 'competition'):
                if not row[field] or len(row[field]) > 150 or any(ord(c) < 32 for c in row[field]):
                    raise ValueError('Team and competition names must contain 1–150 printable characters')
            if normalized(row['home']) == normalized(row['away']):
                raise ValueError('Home and away teams must differ')
            kickoff = utc_datetime(row['kickoff_sast'], 'kickoff_sast', current)
            observed = utc_datetime(row['observed_at'], 'observed_at', current)
            if observed <= kickoff:
                raise ValueError('Result observation must be after kickoff')
            metrics = {}
            for field in METRICS:
                text = row.get(field, '')
                if not text and field in OPTIONAL:
                    metrics[field] = None
                    continue
                try:
                    value = float(text) if field.startswith('xg_') else int(text)
                except ValueError:
                    raise ValueError(f'{field} must be a nonnegative number (whole number for counts)') from None
                if not math.isfinite(value) or value < 0 or value > 1000:
                    raise ValueError(f'{field} must be finite and between 0 and 1,000')
                metrics[field] = value
            for side in ('home', 'away'):
                half = metrics['ht_goals_' + side]
                if half is not None and half > metrics['goals_' + side]:
                    raise ValueError('Half-time goals cannot exceed full-time goals')
            identity = json.dumps([normalized(row[field]) for field in ('home', 'away', 'competition')] + [kickoff.isoformat()])
            digest = hashlib.sha256(json.dumps([identity, source, source_url, observed.isoformat(), metrics], sort_keys=True).encode()).hexdigest()
            records.append({'home': row['home'], 'away': row['away'], 'competition': row['competition'],
                            'kickoff': kickoff.isoformat(), 'identity': identity, 'source': source,
                            'source_url': source_url, 'observed_at': observed.isoformat(),
                            'metrics': metrics, 'digest': digest})
        except ValueError as error:
            raise ValueError(f'Row {index}: {error}') from None
    if not records:
        raise ValueError('CSV contains no historical results')
    return records


def ingest(connection, records, imported_at):
    inserted = duplicates = 0
    for record in records:
        if connection.execute('SELECT 1 FROM historical_observations WHERE digest=?', (record['digest'],)).fetchone():
            duplicates += 1
            continue
        connection.execute('''INSERT OR IGNORE INTO historical_matches
          (identity,home,away,competition,home_key,away_key,competition_key,kickoff_utc) VALUES (?,?,?,?,?,?,?,?)''',
          (record['identity'], record['home'], record['away'], record['competition'], normalized(record['home']),
           normalized(record['away']), normalized(record['competition']), record['kickoff']))
        match_id = connection.execute('SELECT id FROM historical_matches WHERE identity=?', (record['identity'],)).fetchone()[0]
        connection.execute('''INSERT INTO historical_observations
          (match_id,source,source_url,observed_at,imported_at,metrics,digest) VALUES (?,?,?,?,?,?,?)''',
          (match_id, record['source'], record['source_url'], record['observed_at'], imported_at,
           json.dumps(record['metrics'], sort_keys=True), record['digest']))
        inserted += 1
    return {'inserted': inserted, 'duplicates': duplicates}


def observations(connection, match_id, as_of=None):
    params = [match_id]
    query = 'SELECT * FROM historical_observations WHERE match_id=?'
    if as_of:
        # A user-declared timestamp cannot backdate the machine's knowledge.
        query += ' AND observed_at<=? AND imported_at<=?'
        params += [as_of, as_of]
    rows = connection.execute(query + ' ORDER BY observed_at DESC, id DESC', params).fetchall()
    return [{'id': row['id'], 'source': row['source'], 'source_url': row['source_url'],
             'observed_at': row['observed_at'], 'imported_at': row['imported_at'],
             'available_at': max(row['observed_at'], row['imported_at']),
             'metrics': json.loads(row['metrics']),
             'observation_basis':'application_retrieval' if row['source'].startswith('OpenFootball') else 'declared_source_time',
             'reliability': 'CC0 community report · unverified' if row['source'].startswith('OpenFootball') else 'Manual export · source not independently verified'} for row in rows]


def resolve(items):
    # Only the latest timestamp for each source is current. Tied, differing
    # observations stay in contention instead of silently selecting one.
    latest = {}
    for item in items:
        source_key = normalized(item['source'])
        latest[source_key] = max(latest.get(source_key, ''), item['observed_at'])
    current = [item for item in items if item['observed_at'] == latest[normalized(item['source'])]]
    metrics, conflicts = {}, []
    for field in METRICS:
        values = {item['metrics'][field] for item in current if item['metrics'][field] is not None}
        metrics[field] = next(iter(values)) if len(values) == 1 else None
        if len(values) > 1:
            conflicts.append(field)
    return {'metrics': metrics, 'conflicts': conflicts,
            'missing': [field for field in METRICS if metrics[field] is None and field not in conflicts],
            'observation_count': len(items), 'source_count': len(latest),
            'reliability': 'Conflicting data' if conflicts else 'CC0 community reports · unverified' if items and all(i['source'].startswith('OpenFootball') for i in items) else 'Manual export · unverified'}


def match_list(connection, limit=100, offset=0, team=None):
    params = []
    where = ''
    if team:
        where = 'WHERE home_key=? OR away_key=?'
        params = [normalized(team), normalized(team)]
    count = connection.execute('SELECT COUNT(*) FROM historical_matches ' + where, params).fetchone()[0]
    rows = connection.execute('SELECT * FROM historical_matches ' + where + ' ORDER BY kickoff_utc DESC,id DESC LIMIT ? OFFSET ?', params + [limit, offset]).fetchall()
    items = []
    for row in rows:
        resolved = resolve(observations(connection, row['id']))
        items.append({'id': row['id'], 'home': row['home'], 'away': row['away'],
                      'competition': row['competition'], 'kickoff_sast': datetime.fromisoformat(row['kickoff_utc']).astimezone(SAST).isoformat(), **resolved})
    return {'total': count, 'items': items, 'limit': limit, 'offset': offset}


def team_summary(connection, team, current):
    rows = connection.execute('''SELECT * FROM historical_matches WHERE (home_key=? OR away_key=?)
        AND kickoff_utc<? AND EXISTS (SELECT 1 FROM historical_observations o
        WHERE o.match_id=historical_matches.id AND o.observed_at<=? AND o.imported_at<=?)
        ORDER BY kickoff_utc DESC,id DESC LIMIT 20''',
        (normalized(team), normalized(team), current, current, current)).fetchall()
    results = []
    excluded = 0
    for row in rows:
        state = resolve(observations(connection, row['id'], current))
        side = 'home' if row['home_key'] == normalized(team) else 'away'
        opponent = 'away' if side == 'home' else 'home'
        if state['metrics']['goals_home'] is None or state['metrics']['goals_away'] is None:
            excluded += 1
            continue
        metrics = state['metrics']
        gf, ga = metrics['goals_' + side], metrics['goals_' + opponent]
        results.append({'venue': side, 'goals_for': gf, 'goals_against': ga, 'result': 'W' if gf > ga else 'L' if gf < ga else 'D',
                        'xg_for': metrics['xg_' + side], 'xg_against': metrics['xg_' + opponent]})
    windows = {}
    for window in (5, 10, 20):
        selected = results[:window]
        count = len(selected)
        windows[str(window)] = {'sample': count, 'complete_window': count == window,
            'form': [r['result'] for r in selected],
            'goals_for_per_match': sum(r['goals_for'] for r in selected) / count if count else None,
            'goals_against_per_match': sum(r['goals_against'] for r in selected) / count if count else None,
            'clean_sheets': sum(r['goals_against'] == 0 for r in selected) if count else None,
            'without_scoring': sum(r['goals_for'] == 0 for r in selected) if count else None}
    venues = {}
    for venue in ('home', 'away'):
        selected = [r for r in results if r['venue'] == venue]
        venues[venue] = {'sample': len(selected), 'goals_for_per_match': sum(r['goals_for'] for r in selected)/len(selected) if selected else None,
                         'goals_against_per_match': sum(r['goals_against'] for r in selected)/len(selected) if selected else None}
    return {'team': team, 'windows': windows, 'venues': venues, 'excluded_conflicts': excluded,
            'scope': 'Up to 20 most recent stored matches. Descriptive, unadjusted statistics; not opponent-adjusted predictions.',
            'reliability': 'Insufficient for prediction · sources unverified'}
