"""
map-search · Flask search API
==============================
Serves a full-text + intent-aware search engine over a SQLite FTS5 index built
from an OSM/Orbis NexVentura PBF file (see build_index.py).

Architecture
------------
- SQLite FTS5 index (search.db) built offline by build_index.py
- Two scoring modes toggled per request:
    V1  BM25 + small popularity boost + log-distance penalty
    V2  BM25 + name-match boost + place-type boost + category boost
        + confidence boost + popularity boost − log-distance penalty
- Two-tier intent detection before falling back to standard FTS:
    Tier 1  Specific travel intents (park/hotel/terminal/car_rental/eat/taxi/train)
            triggered by transport keywords; resolves a landmark anchor then does
            proximity search within a category-specific radius.
    Tier 2  General POI-category intents ("[category] near [place]")
            supports EN / NL / FR vocabulary; generates related-category chips.
- /api/check_reservation  Async endpoint called by the UI for food POIs that
    have a website; detects known booking platforms (TheFork, OpenTable, …) in
    the URL or the fetched page HTML and returns a direct reservation_url.

Endpoints
---------
GET /                       UI (Leaflet map + sidebar)
GET /api/status             Index readiness + feature counts
GET /api/search             Full-text + intent search
    ?q=<query>
    &limit=<1-50>           default 10
    &type=<feature_type>    filter: address | poi | place | named
    &category=<cat>         filter: amenity value (e.g. restaurant)
    &lat=<float>            user latitude for distance scoring
    &lon=<float>            user longitude for distance scoring
    &ranking=v1|v2          scoring mode (default v1)
GET /api/reverse            Nearest features to a lat/lon point
    ?lat=<float>&lon=<float>&limit=<1-20>
GET /api/check_reservation  Check if a restaurant website supports online booking
    ?url=<website_url>
    Returns {reservable, platform, source, reservation_url}
"""

from flask import Flask, request, jsonify, render_template
import sqlite3
import math
import re
import os
import urllib.request
import urllib.error
from urllib.parse import urlparse

app = Flask(__name__)

DB_FILE = os.environ.get('DB_FILE', os.path.join(os.path.dirname(__file__), 'search.db'))

# ── Place-type ranking weights (v2) ──────────────────────────────────────────
# Higher = stronger boost toward the top of results.
# Ensures that a city named "Gent" ranks above a farm whose addr:city = "Gent".
PLACE_TYPE_BOOST = {
    'country': 20, 'state': 18, 'province': 16, 'region': 15,
    'city': 14, 'municipality': 13, 'borough': 10, 'town': 9,
    'suburb': 7, 'district': 7, 'village': 5, 'neighbourhood': 4,
    'locality': 3, 'hamlet': 3, 'isolated_dwelling': 2,
    'island': 2, 'islet': 1, 'square': 1, 'farm': 0,
}

# ── Category ranking boosts (v2) ─────────────────────────────────────────────
# Transport hubs and major facilities get an extra lift so they surface first
# when a user types their name directly (e.g. "Brussels Airport").
CATEGORY_BOOST = {
    'aerodrome': 12, 'terminal': 8,
    'railway_station': 8, 'bus_station': 6,
    'hospital': 5, 'university': 4,
}

# ── Specific travel intents ───────────────────────────────────────────────────
# Tier-1 intents are triggered when the query contains one of the listed
# keywords in any language (EN/NL/FR).  The remaining tokens are used to find
# a landmark anchor via FTS, then a proximity search is done within the
# configured radius for the relevant OSM categories.

SPECIFIC_INTENT_KEYWORDS = {
    'park':       ['parking', 'park', 'parkeer', 'parkeren', 'stationner', 'garage', 'parkeergelegenheid'],
    'hotel':      ['hotel', 'stay', 'sleep', 'overnight', 'accommodation', 'logement', 'verblijf',
                   'nacht', 'lodge', 'inn', 'hostel'],
    'terminal':   ['terminal', 'departure', 'departures', 'arrival', 'arrivals',
                   'checkin', 'check-in', 'gate', 'boarding', 'pier', 'vertrek', 'aankomst'],
    'car_rental': ['car rental', 'rental', 'hire car', 'autoverhuur', 'location voiture',
                   'rent a car', 'renta'],
    'eat':        ['restaurant', 'eat', 'food', 'coffee', 'cafe', 'lunch',
                   'dinner', 'breakfast', 'drink', 'bar', 'eten', 'drinken'],
    'taxi':       ['taxi', 'cab', 'transfer', 'shuttle', 'uber', 'lyft'],
    'train':      ['train', 'tram', 'metro', 'bus', 'trein', 'spoor', 'transport'],
}

# OSM category values searched by proximity_search for each intent
SPECIFIC_INTENT_CATEGORIES = {
    'park':       ['parking'],
    'hotel':      ['hotel', 'apartment', 'hostel', 'motel', 'guest_house', 'chalet'],
    'car_rental': ['car_rental'],
    'eat':        ['restaurant', 'cafe', 'bar', 'fast_food', 'food_court', 'bakery'],
    'taxi':       ['taxi'],
    'train':      ['railway_station', 'bus_station', 'station', 'subway_entrance'],
}

# Radius in km used by proximity_search for each intent
SPECIFIC_INTENT_RADIUS = {
    'park': 3.0, 'hotel': 6.0, 'car_rental': 2.0,
    'eat': 1.0, 'taxi': 2.0, 'train': 5.0, 'terminal': 3.0,
}

# Display metadata (label, icon, radius_km) for each specific intent
SPECIFIC_INTENT_META = {
    'park':       ('Parking near',               '🅿️',  3.0),
    'hotel':      ('Hotels & accommodation near', '🏨',  6.0),
    'terminal':   ('Terminals at',               '✈️',  3.0),
    'car_rental': ('Car rental at',              '🚗',  2.0),
    'eat':        ('Restaurants & cafes near',   '🍽️', 1.0),
    'taxi':       ('Taxi & transfers at',        '🚕',  2.0),
    'train':      ('Train & transport near',     '🚆',  5.0),
}

# ── General POI category intent ───────────────────────────────────────────────
# Tier-2 intent: detects "[category] near/in [place]" patterns using this map.
# Keys are matched greedily (longest first) against the lowercased query.
# Value tuple: (db_categories, icon, display_label)
# Set db_categories to None for connector-only words that should be ignored.
POI_CATEGORY_MAP = {
    # ── Multi-word entries (must come before single-word to win greedy match) ──
    'gas station':        (['fuel'],                                  '⛽', 'Fuel stations'),
    'petrol station':     (['fuel'],                                  '⛽', 'Fuel stations'),
    'benzinestation':     (['fuel'],                                  '⛽', 'Tankstations'),
    'station essence':    (['fuel'],                                  '⛽', 'Stations-service'),
    'charging station':   (['charging_location', 'charging_station'], '⚡', 'Charging stations'),
    'ev charging':        (['charging_location', 'charging_station'], '⚡', 'EV charging'),
    'car rental':         (['car_rental'],                            '🚗', 'Car rental'),
    'autoverhuur':        (['car_rental'],                            '🚗', 'Autoverhuur'),
    'location voiture':   (['car_rental'],                            '🚗', 'Location de voitures'),
    'fast food':          (['fast_food', 'restaurant'],               '🍔', 'Fast food'),
    'post office':        (['post_office'],                           '📮', 'Post offices'),
    'place of worship':   (['place_of_worship'],                      '⛪', 'Places of worship'),
    'in de buurt':        (None, None, None),   # Dutch connector phrase — skip
    # ── Single-word entries (EN) ──────────────────────────────────────────────
    'restaurant':         (['restaurant'],                            '🍽️', 'Restaurants'),
    'restaurants':        (['restaurant'],                            '🍽️', 'Restaurants'),
    'eatery':             (['restaurant'],                            '🍽️', 'Restaurants'),
    'cafe':               (['cafe'],                                  '☕', 'Cafes'),
    'coffee':             (['cafe'],                                  '☕', 'Cafes'),
    'coffeeshop':         (['cafe'],                                  '☕', 'Cafes'),
    'bar':                (['bar'],                                   '🍺', 'Bars'),
    'pub':                (['bar'],                                   '🍺', 'Bars & pubs'),
    'bakery':             (['bakery'],                                '🥐', 'Bakeries'),
    'pharmacy':           (['pharmacy'],                              '💊', 'Pharmacies'),
    'pharmacies':         (['pharmacy'],                              '💊', 'Pharmacies'),
    'doctor':             (['doctors', 'clinic'],                     '🩺', 'Doctors'),
    'doctors':            (['doctors', 'clinic'],                     '🩺', 'Doctors'),
    'gp':                 (['doctors'],                               '🩺', 'GPs'),
    'clinic':             (['clinic', 'doctors'],                     '🏥', 'Clinics'),
    'hospital':           (['hospital'],                              '🏥', 'Hospitals'),
    'dentist':            (['dentist'],                               '🦷', 'Dentists'),
    'bank':               (['bank'],                                  '🏦', 'Banks'),
    'atm':                (['atm', 'bank'],                           '💳', 'ATMs'),
    'cashpoint':          (['atm'],                                   '💳', 'ATMs'),
    'parking':            (['parking'],                               '🅿️', 'Parking'),
    'fuel':               (['fuel'],                                  '⛽', 'Fuel stations'),
    'petrol':             (['fuel'],                                  '⛽', 'Fuel stations'),
    'garage':             (['parking', 'car_repair'],                 '🔧', 'Garages'),
    'hotel':              (['hotel', 'motel', 'hostel', 'guest_house'], '🏨', 'Hotels'),
    'hotels':             (['hotel', 'motel', 'hostel', 'guest_house'], '🏨', 'Hotels'),
    'hostel':             (['hostel', 'guest_house'],                 '🏨', 'Hostels'),
    'supermarket':        (['supermarket', 'convenience'],            '🛒', 'Supermarkets'),
    'grocery':            (['supermarket', 'convenience'],            '🛒', 'Grocery stores'),
    'school':             (['school'],                                '🎓', 'Schools'),
    'university':         (['university'],                            '🎓', 'Universities'),
    'toilet':             (['toilets'],                               '🚻', 'Toilets'),
    'toilets':            (['toilets'],                               '🚻', 'Toilets'),
    'taxi':               (['taxi'],                                  '🚕', 'Taxis'),
    'charging':           (['charging_location', 'charging_station'], '⚡', 'Charging stations'),
    'gym':                (['gym', 'fitness_centre'],                 '💪', 'Gyms'),
    'fitness':            (['gym', 'fitness_centre'],                 '💪', 'Fitness centres'),
    'cinema':             (['cinema'],                                '🎬', 'Cinemas'),
    'museum':             (['museum'],                                '🏛️', 'Museums'),
    'church':             (['place_of_worship'],                      '⛪', 'Churches'),
    # ── NL ───────────────────────────────────────────────────────────────────
    'koffie':             (['cafe'],                                  '☕', 'Koffiezaken'),
    'apotheek':           (['pharmacy'],                              '💊', 'Apotheken'),
    'huisarts':           (['doctors'],                               '🩺', 'Huisartsen'),
    'ziekenhuis':         (['hospital'],                              '🏥', 'Ziekenhuizen'),
    'tandarts':           (['dentist'],                               '🦷', 'Tandartsen'),
    'supermarkt':         (['supermarket'],                           '🛒', 'Supermarkten'),
    'benzine':            (['fuel'],                                  '⛽', 'Tankstations'),
    'bioscoop':           (['cinema'],                                '🎬', 'Bioscopen'),
    'kerk':               (['place_of_worship'],                      '⛪', 'Kerken'),
    'opladen':            (['charging_location', 'charging_station'], '⚡', 'Laadpalen'),
    'bakker':             (['bakery'],                                '🥐', 'Bakkerijen'),
    'slager':             (['butcher'],                               '🥩', 'Slagers'),
    # ── FR ───────────────────────────────────────────────────────────────────
    'pharmacie':          (['pharmacy'],                              '💊', 'Pharmacies'),
    'médecin':            (['doctors'],                               '🩺', 'Médecins'),
    'clinique':           (['clinic'],                                '🏥', 'Cliniques'),
    'dentiste':           (['dentist'],                               '🦷', 'Dentistes'),
    'banque':             (['bank'],                                  '🏦', 'Banques'),
    'boulangerie':        (['bakery'],                                '🥐', 'Boulangeries'),
    'supermarché':        (['supermarket'],                           '🛒', 'Supermarchés'),
    'essence':            (['fuel'],                                  '⛽', 'Stations-service'),
    'cinéma':             (['cinema'],                                '🎬', 'Cinémas'),
    'église':             (['place_of_worship'],                      '⛪', 'Églises'),
}

# Groups used to generate "related" suggestion chips after a category intent.
# When a user searches "pharmacy near Gent", chips for other health POIs appear.
CATEGORY_GROUPS = {
    'health':        ['pharmacy', 'doctors', 'clinic', 'hospital', 'dentist'],
    'food':          ['restaurant', 'cafe', 'bar', 'fast_food', 'bakery'],
    'transport':     ['parking', 'fuel', 'car_rental', 'taxi', 'charging_location'],
    'accommodation': ['hotel', 'motel', 'hostel', 'guest_house'],
    'finance':       ['bank', 'atm'],
    'shopping':      ['supermarket', 'convenience', 'butcher'],
    'culture':       ['museum', 'cinema', 'place_of_worship'],
    'fitness':       ['gym', 'fitness_centre'],
}

# Words that connect a category to a location but carry no anchor meaning.
# Stripped before running the anchor FTS query.
CONNECTOR_WORDS = {
    'near', 'nearest', 'closest', 'nearby', 'in', 'at', 'around', 'by', 'within',
    'close', 'next', 'to',
    'dichtbij', 'naast', 'bij', 'rond',
    'près', 'proche', 'autour',
}

# Additional words to remove from the anchor query (articles, prepositions, …).
ANCHOR_STOPWORDS = {
    'near', 'nearest', 'closest', 'nearby', 'at', 'to', 'in', 'for', 'from', 'the', 'a', 'an',
    'de', 'het', 'du', 'au', 'la', 'le', 'les', 'bij', 'aan', 'naar',
    'dichtbij', 'close', 'next', 'around', 'by', 'of',
}

# Contextual notes shown in the intent banner for known airports.
AIRPORT_NOTES = {
    'brussels airport': (
        'Brussels Airport has 1 terminal. '
        'Pier A (Schengen) · Pier B (non-Schengen/intercontinental). '
        'Train to Brussels-Central ≈17 min (Diabolo line).'
    ),
    'luchthaven brussel nationaal': (
        'Pier A (Schengen) · Pier B (niet-Schengen). '
        'Trein naar Brussel-Centraal ≈17 min.'
    ),
    'brussels south charleroi airport': (
        'Charleroi (CRL) has Terminal A and Terminal B. '
        'Bus to Brussels-South ≈60 min (Flibco/TEC).'
    ),
}


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    """Open a read-optimised SQLite connection with row-dict factory."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA cache_size=-64000')   # 64 MB page cache
    return conn


def haversine(lat1, lon1, lat2, lon2):
    """Return great-circle distance in metres between two WGS-84 points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def build_fts_query(q):
    """
    Convert a free-text query into an FTS5 MATCH expression.

    Each token is wrapped in double-quotes and given a prefix wildcard (*) so
    partial words still match.  Special FTS5 punctuation is stripped first to
    avoid syntax errors.
    """
    tokens = q.split()
    escaped = []
    for t in tokens:
        safe = re.sub(r'["\'\-\(\)\{\}\[\]\^\~\*\?\\:!]', ' ', t).strip()
        if safe:
            escaped.append(f'"{safe}"*')
    return ' '.join(escaped) if escaped else '""'


def build_label(d):
    """
    Build a human-readable display label from a feature dict.

    Format: Name, [housenumber street,] city postcode
    Falls back to the feature id if nothing else is available.
    """
    parts = []
    if d['name']:
        parts.append(d['name'])
    addr_parts = []
    if d['housenumber']:
        addr_parts.append(d['housenumber'])
    if d['street']:
        addr_parts.append(d['street'])
    if addr_parts:
        parts.append(', '.join(addr_parts))
    if d['city']:
        parts.append(d['city'])
    if d['postcode']:
        parts.append(d['postcode'])
    return ', '.join(parts) if parts else d.get('id', '')


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_v1(r, query_tokens, lat, lon):
    """
    V1 ranking: BM25 score + small popularity boost − log distance penalty.

    Lower score = better rank.  Distance penalty only applies when a user
    location is provided.
    """
    fts_score    = abs(float(r['fts_rank']))
    pop_boost    = float(r['popularity'] or 0) * 0.5
    dist_km      = (r['distance_m'] / 1000.0) if r['distance_m'] is not None else 0
    dist_penalty = math.log1p(dist_km) if lat is not None else 0
    return round(fts_score - pop_boost + dist_penalty, 4)


def score_v2(r, query_tokens, lat, lon):
    """
    V2 ranking: multi-signal score combining BM25 with semantic boosts.

    Components (all subtracted from BM25 so lower = better):
      name_boost      — token hits in the feature name (×3) + full-match bonus (+5)
      place_boost     — OSM place type weight (city=14 down to farm=0)
      category_boost  — major facility bonus (aerodrome=12, station=8, …)
      conf_boost      — Orbis confidence:feature value ×2
      pop_boost       — log(popularity+1) ×1.5
      dist_penalty    — log(distance_km+1), added (makes score worse with distance)
    """
    fts_score      = abs(float(r['fts_rank']))
    name           = (r['name'] or '').lower()
    name_hits      = sum(1 for t in query_tokens if t in name)
    full_match     = name == ' '.join(query_tokens).lower()
    name_boost     = name_hits * 3.0 + (5.0 if full_match else 0.0)
    place_boost    = PLACE_TYPE_BOOST.get(r['place_type'] or '', 0)
    category_boost = CATEGORY_BOOST.get(r['category'] or '', 0)
    conf_boost     = float(r['confidence'] or 0.5) * 2.0
    pop_boost      = math.log1p(float(r['popularity'] or 0)) * 1.5
    dist_penalty   = (math.log1p(r['distance_m'] / 1000.0)
                      if lat is not None and r['distance_m'] is not None else 0)
    return round(fts_score - name_boost - place_boost - category_boost
                 - conf_boost - pop_boost + dist_penalty, 4)


# ── Intent detection ──────────────────────────────────────────────────────────

def detect_specific_intent(tokens):
    """
    Tier-1 intent detection: airport / transport-hub context.

    Scans the query for transport keywords (parking, hotel, terminal, …).
    Returns (intent_key, anchor_tokens) where anchor_tokens are the remaining
    tokens after stripping the matched keyword and stopwords.
    Returns (None, tokens) if no specific intent is found.
    """
    q_lower = ' '.join(tokens).lower()
    for intent, keywords in SPECIFIC_INTENT_KEYWORDS.items():
        # Try longest keywords first to avoid partial matches (e.g. "car rental" before "car")
        for kw in sorted(keywords, key=len, reverse=True):
            if kw in q_lower:
                kw_parts = set(kw.split())
                remaining = [t for t in tokens
                             if t.lower() not in kw_parts
                             and t.lower() not in ANCHOR_STOPWORDS]
                return intent, remaining
    return None, tokens


def detect_category_intent(tokens):
    """
    Tier-2 intent detection: general "[category] near [place]" patterns.

    Tries each POI_CATEGORY_MAP key (longest first) against the lowercased
    query.  When a match is found, strips category tokens, connectors, and
    stopwords to obtain the anchor location.  Generates related-category
    suggestion chips from the same CATEGORY_GROUP.

    Returns (categories, icon, label, anchor_tokens, suggestions) or None if
    no match is found or no anchor remains after stripping.
    """
    q_lower = ' '.join(tokens).lower()

    for kw in sorted(POI_CATEGORY_MAP.keys(), key=len, reverse=True):
        if kw not in q_lower:
            continue
        cats, icon, label = POI_CATEGORY_MAP[kw]
        if cats is None:
            continue   # connector-only entry, ignore

        kw_parts = set(kw.split())
        remaining = [t for t in tokens
                     if t.lower() not in kw_parts
                     and t.lower() not in CONNECTOR_WORDS
                     and t.lower() not in ANCHOR_STOPWORDS]

        if not remaining:
            return None   # no anchor — can't do proximity search

        # Build related-category suggestion chips from the same group
        anchor_str = ' '.join(remaining)
        suggestions = []
        seen_labels = {label}
        for group_cats in CATEGORY_GROUPS.values():
            if not any(c in group_cats for c in cats):
                continue
            for term, (gcats, gicon, glabel) in POI_CATEGORY_MAP.items():
                if (gcats and any(c in group_cats for c in gcats)
                        and glabel not in seen_labels
                        and len(term.split()) == 1):
                    suggestions.append({
                        'label': f'{gicon} {glabel}',
                        'q':     f'{term} near {anchor_str}',
                    })
                    seen_labels.add(glabel)
                    if len(suggestions) >= 4:
                        break
            if len(suggestions) >= 4:
                break

        return cats, icon, label, remaining, suggestions

    return None


def best_anchor(rows):
    """
    Select the best landmark from a list of FTS candidates.

    Scoring heuristic (higher = better anchor):
      +20  aerodrome
      +10  terminal / railway_station / bus_station
      +5   poi or named feature type
      +2   place feature type
      +confidence × 3
      +popularity × 2

    Returns a feature dict (with distance_m=None and label set) or None if no
    candidate has a valid location.
    """
    def anchor_score(r):
        cat        = r['category'] or ''
        ftype      = r['feature_type'] or ''
        cat_bonus  = (20 if cat == 'aerodrome'
                      else 10 if cat in ('terminal', 'railway_station', 'bus_station')
                      else 0)
        type_bonus = 5 if ftype in ('poi', 'named') else (2 if ftype == 'place' else 0)
        # Negative so candidates.sort() puts best anchor first
        return -(cat_bonus + type_bonus
                 + float(r['confidence'] or 0) * 3
                 + float(r['popularity'] or 0) * 2)

    candidates = [dict(r) for r in rows if r['lat'] is not None]
    if not candidates:
        return None
    candidates.sort(key=anchor_score)
    best = candidates[0]
    best['distance_m'] = None
    best['label'] = build_label(best)
    return best


def proximity_search(conn, categories, lat, lon, radius_km, limit):
    """
    Find features of given OSM categories within radius_km of (lat, lon).

    Uses a bounding-box pre-filter for speed, then applies an exact haversine
    check to discard corners.  Results are sorted by
    (distance_km − log(popularity+1)×0.5 − confidence).
    """
    d_lat = radius_km / 111.0
    d_lon = radius_km / (111.0 * math.cos(math.radians(lat)))
    ph    = ','.join('?' * len(categories))
    rows  = conn.execute(
        f"""SELECT id, feature_type, name, housenumber, street, city, postcode,
                   category, place_type, phone, website, lat, lon, confidence, popularity
            FROM features
            WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
              AND category IN ({ph}) AND lat IS NOT NULL
            LIMIT 500""",
        (lat - d_lat, lat + d_lat, lon - d_lon, lon + d_lon, *categories)
    ).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        d['distance_m'] = round(haversine(lat, lon, r['lat'], r['lon']))
        if d['distance_m'] > radius_km * 1000:
            continue   # outside the actual circle
        d['label']    = build_label(d)
        d['fts_rank'] = 0
        d['score']    = round(d['distance_m'] / 1000.0
                              - math.log1p(float(d['popularity'] or 0)) * 0.5
                              - float(d['confidence'] or 0.5), 4)
        results.append(d)

    results.sort(key=lambda x: x['score'])
    return results[:limit]


def run_intent(conn, intent_type, categories, radius_km,
               anchor_query, label, icon, suggestions, limit, note=None):
    """
    Execute an intent: resolve anchor via FTS → proximity search.

    Steps:
      1. Build an FTS query from anchor_query and fetch the top-20 candidates.
      2. Score them with best_anchor() to pick the most relevant landmark.
      3. Run proximity_search() within radius_km of the anchor.
      4. For 'terminal' intent, prepend the anchor itself to the results.

    Returns (results, intent_meta) on success, or (None, None) if no anchor
    with a valid location is found.
    """
    fts_q = build_fts_query(anchor_query)
    try:
        rows = conn.execute(
            """SELECT f.id, f.feature_type, f.name, f.housenumber, f.street,
                      f.city, f.postcode, f.category, f.place_type,
                      f.phone, f.website, f.lat, f.lon, f.confidence, f.popularity,
                      rank AS fts_rank
               FROM search_idx si JOIN features f ON f.rowid = si.rowid
               WHERE search_idx MATCH ? ORDER BY rank LIMIT 20""",
            (fts_q,)
        ).fetchall()
    except Exception:
        return None, None

    anchor = best_anchor(rows)
    if not anchor or not anchor['lat']:
        return None, None

    results = proximity_search(conn, categories, anchor['lat'], anchor['lon'],
                               radius_km, limit)

    if intent_type == 'terminal':
        # Always show the airport/terminal node itself at the top
        anchor_copy = dict(anchor)
        anchor_copy['score'] = -999
        results = [anchor_copy] + [r for r in results if r['id'] != anchor['id']][:limit - 1]

    intent_meta = {
        'type':        intent_type,
        'label':       label,
        'icon':        icon,
        'anchor':      {'name': anchor['name'], 'lat': anchor['lat'],
                        'lon': anchor['lon'], 'id': anchor['id']},
        'note':        note,
        'suggestions': suggestions,
    }
    return results[:limit], intent_meta


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    """Serve the single-page Leaflet UI."""
    return render_template('index.html')


@app.route('/api/status')
def status():
    """
    Return index readiness, total feature count, per-type breakdown, and DB size.

    Used by the UI status badge on startup.
    """
    if not os.path.exists(DB_FILE):
        return jsonify({'ready': False, 'message': 'Index not built yet.'})
    try:
        conn  = get_db()
        rows  = conn.execute(
            "SELECT feature_type, COUNT(*) AS cnt FROM features GROUP BY feature_type"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        conn.close()
        return jsonify({'ready': True, 'total': total,
                        'by_type': {r['feature_type']: r['cnt'] for r in rows},
                        'db_size_mb': round(os.path.getsize(DB_FILE) / 1024 / 1024, 1)})
    except Exception as e:
        return jsonify({'ready': False, 'message': str(e)})


@app.route('/api/search')
def search():
    """
    Main search endpoint.

    Query parameters
    ----------------
    q        : search string (required)
    limit    : max results to return, 1-50 (default 10)
    type     : filter by feature_type (address | poi | place | named)
    category : filter by OSM category value
    lat, lon : user coordinates for distance scoring
    ranking  : v1 (BM25+pop) or v2 (multi-signal, default v1)

    Processing order
    ----------------
    1. Tier-1 specific intent detection (transport keywords + landmark anchor)
    2. Tier-2 general POI-category intent ("[category] near [place]")
    3. Standard FTS5 search with V1 or V2 scoring
    """
    q        = request.args.get('q', '').strip()
    limit    = min(int(request.args.get('limit', 10)), 50)
    ftype    = request.args.get('type', '')
    category = request.args.get('category', '')
    lat_s    = request.args.get('lat', '')
    lon_s    = request.args.get('lon', '')
    ranking  = request.args.get('ranking', 'v1')

    if not q:
        return jsonify({'results': [], 'query': q})
    if not os.path.exists(DB_FILE):
        return jsonify({'error': 'Index not ready.'}), 503

    try:
        lat = float(lat_s) if lat_s else None
        lon = float(lon_s) if lon_s else None
    except ValueError:
        lat = lon = None

    tokens = [t for t in q.split() if t]
    conn   = get_db()

    # ── 1. Specific travel intents (park/hotel/terminal/…) ───────────────────
    intent, anchor_tokens = detect_specific_intent(tokens)
    if intent and anchor_tokens:
        label, icon, radius = SPECIFIC_INTENT_META[intent]
        categories = SPECIFIC_INTENT_CATEGORIES.get(intent,
                         ['railway_station', 'bus_station', 'aerodrome', 'terminal'])
        anchor_q = ' '.join(anchor_tokens)

        # Attach airport layout note for terminal queries at known airports
        note = None
        if intent == 'terminal':
            for key, text in AIRPORT_NOTES.items():
                if key in anchor_q.lower():
                    note = text
                    break

        # Related-intent suggestion chips for the intent banner
        related = {
            'park':       [('🏨 Hotels',      'hotel near'),     ('🍽️ Restaurants', 'restaurant near'), ('🚗 Car rental', 'car rental near')],
            'hotel':      [('🅿️ Parking',     'parking near'),   ('🚗 Car rental',  'car rental near'), ('🍽️ Restaurants', 'restaurant near')],
            'terminal':   [('🅿️ Parking',     'parking near'),   ('🏨 Hotels',      'hotel near'),      ('🚗 Car rental', 'car rental near'), ('🚆 Train', 'train near')],
            'car_rental': [('🅿️ Parking',     'parking near'),   ('🏨 Hotels',      'hotel near')],
            'eat':        [('🏨 Stay nearby', 'hotel near'),      ('🅿️ Parking',     'parking near')],
            'taxi':       [('🚆 Train',        'train near'),     ('🅿️ Parking',     'parking near')],
            'train':      [('🚕 Taxi',         'taxi near'),      ('🅿️ Parking',     'parking near'), ('🏨 Hotels', 'hotel near')],
        }
        suggestions = [{'label': l, 'q': f'{p}{anchor_q}'} for l, p in related.get(intent, [])]

        results, intent_meta = run_intent(
            conn, intent, categories, radius, anchor_q, label, icon, suggestions, limit, note
        )
        if results is not None:
            conn.close()
            return jsonify({'results': results, 'query': q,
                            'count': len(results), 'ranking': ranking,
                            'intent': intent_meta})

    # ── 2. General POI-category intent ("[restaurant] near [Gent]") ──────────
    cat_result = detect_category_intent(tokens)
    if cat_result:
        cats, icon, label, anchor_tokens, suggestions = cat_result
        anchor_q = ' '.join(anchor_tokens)
        radius   = 2.0   # default 2 km for general POI category searches
        results, intent_meta = run_intent(
            conn, 'poi_category', cats, radius,
            anchor_q, f'{label} near', icon, suggestions, limit
        )
        if results is not None:
            conn.close()
            return jsonify({'results': results, 'query': q,
                            'count': len(results), 'ranking': ranking,
                            'intent': intent_meta})

    # ── 3. Standard FTS search ────────────────────────────────────────────────
    query_tokens_lower = [t.lower() for t in tokens]
    fts_query = build_fts_query(q)
    score_fn  = score_v2 if ranking == 'v2' else score_v1

    try:
        params      = [fts_query]
        extra_where = []
        if ftype:
            extra_where.append("f.feature_type = ?")
            params.append(ftype)
        if category:
            extra_where.append("f.category = ?")
            params.append(category)
        where_clause = ('AND ' + ' AND '.join(extra_where)) if extra_where else ''

        rows = conn.execute(
            f"""SELECT f.id, f.feature_type, f.name, f.housenumber, f.street,
                       f.city, f.postcode, f.category, f.place_type,
                       f.phone, f.website, f.lat, f.lon,
                       f.confidence, f.popularity, rank AS fts_rank
                FROM search_idx si JOIN features f ON f.rowid = si.rowid
                WHERE search_idx MATCH ? {where_clause}
                ORDER BY rank LIMIT 500""",
            params
        ).fetchall()
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e), 'query': q}), 400

    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        d['distance_m'] = (round(haversine(lat, lon, r['lat'], r['lon']))
                           if lat is not None and r['lat'] is not None else None)
        d['label'] = build_label(d)
        d['score'] = score_fn(d, query_tokens_lower, lat, lon)
        if ranking == 'v2':
            name = (d['name'] or '').lower()
            hits = sum(1 for t in query_tokens_lower if t in name)
            fm   = name == ' '.join(query_tokens_lower)
            d['score_detail'] = {
                'bm25':           round(abs(float(d['fts_rank'])), 4),
                'name_boost':     round(hits * 3.0 + (5.0 if fm else 0.0), 2),
                'place_boost':    PLACE_TYPE_BOOST.get(d['place_type'] or '', 0),
                'category_boost': CATEGORY_BOOST.get(d['category'] or '', 0),
                'conf_boost':     round(float(d['confidence'] or 0.5) * 2.0, 2),
                'pop_boost':      round(math.log1p(float(d['popularity'] or 0)) * 1.5, 4),
            }
        results.append(d)

    results.sort(key=lambda x: x['score'])
    return jsonify({'results': results[:limit], 'query': q,
                    'count': len(results[:limit]), 'ranking': ranking, 'intent': None})


@app.route('/api/reverse')
def reverse():
    """
    Reverse geocode: return the nearest features to a lat/lon point.

    Searches within a fixed ~1 km bounding box and returns up to `limit`
    results sorted by distance.
    """
    try:
        lat = float(request.args.get('lat'))
        lon = float(request.args.get('lon'))
    except (TypeError, ValueError):
        return jsonify({'error': 'lat and lon required'}), 400

    limit         = min(int(request.args.get('limit', 5)), 20)
    d_lat, d_lon  = 0.009, 0.013   # ≈ 1 km search box

    if not os.path.exists(DB_FILE):
        return jsonify({'error': 'Index not ready'}), 503

    conn = get_db()
    rows = conn.execute(
        """SELECT id, feature_type, name, housenumber, street, city, postcode,
                  category, place_type, phone, website, lat, lon, confidence, popularity
           FROM features
           WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND lat IS NOT NULL
           LIMIT 200""",
        (lat - d_lat, lat + d_lat, lon - d_lon, lon + d_lon)
    ).fetchall()
    conn.close()

    results = []
    for r in rows:
        d = dict(r)
        d['distance_m'] = round(haversine(lat, lon, r['lat'], r['lon']))
        d['label'] = build_label(d)
        results.append(d)

    results.sort(key=lambda x: x['distance_m'])
    return jsonify({'results': results[:limit], 'lat': lat, 'lon': lon})


# ── Reservation check ─────────────────────────────────────────────────────────
# Known online booking platform domains.  Checked against the restaurant's
# website URL first (instant match), then against href values in the fetched
# page HTML (link match).
BOOKING_PLATFORMS = {
    'thefork.com':        'TheFork',
    'thefork.be':         'TheFork',
    'thefork.nl':         'TheFork',
    'lafourchette.com':   'TheFork',
    'opentable.com':      'OpenTable',
    'opentable.be':       'OpenTable',
    'opentable.nl':       'OpenTable',
    'quandoo.com':        'Quandoo',
    'quandoo.be':         'Quandoo',
    'quandoo.nl':         'Quandoo',
    'bookatable.com':     'Bookatable',
    'zenchef.com':        'Zenchef',
    'resengo.com':        'Resengo',       # Belgium-specific
    'formitable.com':     'Formitable',    # NL/BE
    'covermanager.com':   'CoverManager',
    'resy.com':           'Resy',
    'sevenrooms.com':     'SevenRooms',
    'eat.be':             'Eat.be',
    'resto.be':           'Resto.be',
    'bookingkit.net':     'Bookingkit',
    'planity.com':        'Planity',
}

# Reservation-related keywords detected in fetched page HTML (case-insensitive).
# Used as a fallback when no known platform link is present.
BOOKING_KEYWORDS = [
    'reserveer online', 'online reserveren', 'tafel reserveren', 'reservatie',
    'réserver en ligne', 'réservation en ligne', 'réserver une table',
    'book a table', 'reserve a table', 'online booking', 'online reservation',
    'book online', 'make a reservation', 'table reservation',
]

# In-process cache: url → result dict.  Persists for the lifetime of the server
# process to avoid re-fetching the same restaurant website repeatedly.
_reservation_cache = {}


@app.route('/api/check_reservation')
def check_reservation():
    """
    Determine whether a restaurant website supports online reservation.

    Detection strategy (in order):
      1. URL match  — the website URL itself contains a known booking platform
                      domain (e.g. the restaurant IS a TheFork page).
      2. Link match — fetches the page HTML and looks for hrefs containing a
                      known platform domain; extracts the exact reservation URL.
      3. Keyword    — scans fetched HTML for reservation-related phrases in
                      EN/NL/FR; tries to extract the nearest href as the URL.

    Results are cached in-process.

    Returns JSON: {reservable, platform, source, reservation_url}
      reservable      : bool
      platform        : platform name string or null
      source          : "url" | "link" | "keyword" | null
      reservation_url : direct booking URL (may equal the input URL as fallback)
    """
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'reservable': False}), 400

    if url in _reservation_cache:
        return jsonify(_reservation_cache[url])

    # 1. URL itself is a booking platform page
    url_lower = url.lower()
    for domain, platform in BOOKING_PLATFORMS.items():
        if domain in url_lower:
            result = {'reservable': True, 'platform': platform,
                      'source': 'url', 'reservation_url': url}
            _reservation_cache[url] = result
            return jsonify(result)

    # 2 & 3. Fetch the restaurant's website
    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'Mozilla/5.0 (compatible; MapSearch/1.0)'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            ct = resp.headers.get('Content-Type', '')
            if 'text/html' not in ct:
                raise ValueError('not html')
            # Preserve original-case HTML for href extraction; keep a lowercase copy for searching
            html       = resp.read(60_000).decode('utf-8', errors='ignore')
            html_lower = html.lower()
    except Exception:
        result = {'reservable': False, 'platform': None}
        _reservation_cache[url] = result
        return jsonify(result)

    # 2. Booking platform link found in page HTML
    for domain, platform in BOOKING_PLATFORMS.items():
        if domain in html_lower:
            m = re.search(
                r'href=["\']([^"\']*' + re.escape(domain) + r'[^"\']*)["\']',
                html, re.IGNORECASE)
            res_url = m.group(1) if m else url
            if res_url.startswith('//'):
                res_url = 'https:' + res_url
            elif res_url.startswith('/'):
                p = urlparse(url)
                res_url = f'{p.scheme}://{p.netloc}{res_url}'
            result = {'reservable': True, 'platform': platform,
                      'source': 'link', 'reservation_url': res_url}
            _reservation_cache[url] = result
            return jsonify(result)

    # 3. Reservation keyword found in page HTML
    for kw in BOOKING_KEYWORDS:
        if kw in html_lower:
            # Try to find an href on the same anchor element, or any reservation href
            m = re.search(
                r'href=["\']([^"\']+)["\'][^>]*>[^<]*' + re.escape(kw),
                html_lower)
            if not m:
                m = re.search(
                    r'href=["\']([^"\']+reservation[^"\']*)["\']', html_lower)
            res_url = m.group(1) if m else url
            if res_url.startswith('/'):
                p = urlparse(url)
                res_url = f'{p.scheme}://{p.netloc}{res_url}'
            result = {'reservable': True, 'platform': None,
                      'source': 'keyword', 'reservation_url': res_url or url}
            _reservation_cache[url] = result
            return jsonify(result)

    result = {'reservable': False, 'platform': None}
    _reservation_cache[url] = result
    return jsonify(result)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5017, debug=False)
