#!/usr/bin/env python3
"""
Build SQLite FTS5 search index from Orbis NexVentura PBF file.
Phase 1: Nodes only (addresses, POIs, places) — ~8M entities.

Strategy:
  1. Insert all rows into features table with periodic commits (no FTS trigger)
  2. After all inserts, do a single FTS5 rebuild (much faster than per-row inserts)

Usage:
    python3 build_index.py [pbf_file] [db_file]
"""
import osmium
import sqlite3
import sys
import time
import os

PBF_FILE = sys.argv[1] if len(sys.argv) > 1 else \
    '/Users/grymonpon/documents/ON2611/orbis_nexventura_26110_000_global_bel.osm.pbf'
DB_FILE = sys.argv[2] if len(sys.argv) > 2 else \
    '/Users/grymonpon/map-search/search.db'

# Schema — no trigger, FTS populated via rebuild at the end
SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
    id           TEXT PRIMARY KEY,
    feature_type TEXT NOT NULL,
    name         TEXT,
    housenumber  TEXT,
    street       TEXT,
    city         TEXT,
    postcode     TEXT,
    category     TEXT,
    place_type   TEXT,
    phone        TEXT,
    website      TEXT,
    lat          REAL,
    lon          REAL,
    confidence   REAL DEFAULT 0.5,
    popularity   REAL DEFAULT 0.0,
    search_text  TEXT
);

CREATE INDEX IF NOT EXISTS idx_feature_type ON features(feature_type);
CREATE INDEX IF NOT EXISTS idx_postcode     ON features(postcode);
CREATE INDEX IF NOT EXISTS idx_lat_lon      ON features(lat, lon);
CREATE INDEX IF NOT EXISTS idx_category     ON features(category);

CREATE VIRTUAL TABLE IF NOT EXISTS search_idx USING fts5(
    search_text,
    tokenize = "unicode61 remove_diacritics 1",
    content  = features,
    content_rowid = rowid
);
"""

SKIP_TAGS    = {'routing_node', 'connector', 'traffic_sign', 'display_lane', 'absolute_height'}
CATEGORY_KEYS = ['amenity', 'shop', 'tourism', 'office', 'healthcare', 'leisure', 'craft', 'emergency', 'aeroway', 'public_transport']
LANG_SUFFIXES = ['nl-Latn', 'fr-Latn', 'de-Latn']

INSERT_SQL = (
    'INSERT OR IGNORE INTO features '
    '(id, feature_type, name, housenumber, street, city, postcode, '
    ' category, place_type, phone, website, lat, lon, '
    ' confidence, popularity, search_text) '
    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
)


class IndexBuilder(osmium.SimpleHandler):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.cur  = conn.cursor()
        self.batch = []
        self.batch_size = 10_000
        self.counts = {'address': 0, 'poi': 0, 'place': 0, 'named': 0}
        self.skipped = 0
        self.t0 = time.time()

    def _flush(self):
        if not self.batch:
            return
        self.cur.executemany(INSERT_SQL, self.batch)
        self.conn.commit()
        self.batch = []

        total = sum(self.counts.values())
        if total % 500_000 == 0 and total > 0:
            elapsed = time.time() - self.t0
            rate = total / elapsed
            print(f'  {total:>9,}  ({elapsed:.0f}s, {rate:.0f}/s)  '
                  f'addr={self.counts["address"]:,}  poi={self.counts["poi"]:,}  '
                  f'place={self.counts["place"]:,}',
                  flush=True)

    def node(self, n):
        # Pre-filter: avoid building dict for ~94% of nodes that have nothing searchable.
        # Direct tag membership checks are much faster than dict(n.tags) for nodes
        # with 50-100 tags (common in this Orbis dataset).
        ntags = n.tags
        has_name  = 'name' in ntags
        has_addr  = 'addr:housenumber' in ntags or 'address_point' in ntags
        has_place = 'place' in ntags
        if not (has_name or has_addr or has_place):
            self.skipped += 1
            return

        tags = dict(ntags)

        if tags.get('existence_classification') in ('closed', 'removed'):
            self.skipped += 1
            return

        category  = next((tags[k] for k in CATEGORY_KEYS if k in tags), '')

        if not (has_addr or has_name or has_place):
            self.skipped += 1
            return

        if has_place:
            feature_type = 'place'
        elif has_name and category:
            feature_type = 'poi'
        elif has_addr:
            feature_type = 'address'
        elif has_name:
            feature_type = 'named'
        else:
            self.skipped += 1
            return

        def pick(key):
            return (tags.get(key) or
                    tags.get(f'{key}:nl-Latn') or
                    tags.get(f'{key}:fr-Latn') or
                    tags.get(f'{key}:de-Latn') or '')

        name        = tags.get('name', '')
        housenumber = tags.get('addr:housenumber', '')
        street      = pick('addr:street')
        city        = pick('addr:city')
        postcode    = tags.get('addr:postcode') or tags.get('addr:postcode:nl-Latn', '')
        place_type  = tags.get('place', '')
        phone       = tags.get('phone', '')
        website     = tags.get('website', '')

        try:
            lat = n.location.lat if n.location.valid() else None
            lon = n.location.lon if n.location.valid() else None
        except Exception:
            lat = lon = None

        confidence = float(tags.get('confidence:feature', 0.5))
        popularity = float(tags.get('popularity', 0.0))
        node_id    = tags.get('gers_identifier') or f'n/{n.id}'

        # Build search text
        parts = []
        for v in [name, street, housenumber, city, postcode, category, place_type]:
            if v:
                parts.append(v)
        for lang in LANG_SUFFIXES:
            for base in ['name', 'addr:street', 'addr:city']:
                v = tags.get(f'{base}:{lang}', '')
                if v and v not in parts:
                    parts.append(v)
        for lang in LANG_SUFFIXES:
            v = tags.get(f'short_name:without_prefix:{lang}', '')
            if v and v not in parts:
                parts.append(v)
        if tags.get('alt_name'):
            parts.append(tags['alt_name'])

        search_text = ' '.join(parts)

        self.batch.append((
            node_id, feature_type, name, housenumber, street, city, postcode,
            category, place_type, phone, website, lat, lon,
            confidence, popularity, search_text
        ))
        self.counts[feature_type] += 1

        if len(self.batch) >= self.batch_size:
            self._flush()

    def finish(self):
        self._flush()


def main():
    if os.path.exists(DB_FILE):
        print(f'Removing existing index: {DB_FILE}', flush=True)
        os.remove(DB_FILE)
        for ext in ('-shm', '-wal'):
            p = DB_FILE + ext
            if os.path.exists(p):
                os.remove(p)

    print(f'Creating database: {DB_FILE}', flush=True)
    conn = sqlite3.connect(DB_FILE)
    conn.executescript(SCHEMA)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA cache_size=-256000')
    conn.commit()

    builder = IndexBuilder(conn)

    print(f'Reading: {PBF_FILE}', flush=True)
    t0 = time.time()
    builder.apply_file(PBF_FILE, locations=False)
    builder.finish()

    elapsed = time.time() - t0
    total = sum(builder.counts.values())
    print(f'\nNode pass done in {elapsed:.1f}s  ({total:,} indexed, {builder.skipped:,} skipped)',
          flush=True)
    for k, v in builder.counts.items():
        print(f'  {k}: {v:,}', flush=True)

    print('\nBuilding FTS5 index (bulk rebuild)...', flush=True)
    t1 = time.time()
    conn.execute("INSERT INTO search_idx(search_idx) VALUES('rebuild')")
    conn.commit()
    print(f'FTS5 rebuild done in {time.time()-t1:.1f}s', flush=True)

    print('\nOptimising FTS5 index...', flush=True)
    conn.execute("INSERT INTO search_idx(search_idx) VALUES('optimize')")
    conn.commit()
    conn.close()

    size_mb = os.path.getsize(DB_FILE) / 1024 / 1024
    print(f'\nDone. Index size: {size_mb:.0f} MB  Total time: {time.time()-t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
