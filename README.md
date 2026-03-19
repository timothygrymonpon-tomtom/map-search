# map-search

Intent-aware local map search engine over OSM/Orbis NexVentura PBF data.
Built with Python · Flask · SQLite FTS5 · Leaflet.js

![screenshot](https://img.shields.io/badge/Flask-5017-blue) ![SQLite FTS5](https://img.shields.io/badge/SQLite-FTS5-green)

---

## Features

| Feature | Details |
|---|---|
| **Full-text search** | SQLite FTS5 with `unicode61` tokeniser + diacritic removal |
| **Two ranking modes** | V1: BM25 + popularity · V2: multi-signal (name match, place type, category, confidence, distance) |
| **Tier-1 intent** | Transport-hub context — *"parking near Brussels Airport"*, *"car rental nearest to TomTom Gent"* |
| **Tier-2 intent** | General POI categories — *"pharmacy near Gent"*, *"restaurant in Brugge"* (EN / NL / FR) |
| **Reservation check** | Async check per restaurant result: detects TheFork, OpenTable, Resengo, Zenchef, and 16 other booking platforms; clickable badge links directly to the reservation page |
| **Reverse geocode** | `/api/reverse?lat=…&lon=…` — nearest features to a point |
| **User location** | ⊙ button uses browser Geolocation API for distance-aware results |

---

## Prerequisites

```
Python 3.9+
pip install flask osmium
```

An Orbis NexVentura PBF file (or any OSM PBF).

---

## Quick start

### 1. Build the index

```bash
python3 build_index.py /path/to/region.osm.pbf search.db
```

For the Belgian extract (~110 M nodes) this takes ≈13 minutes and produces a
~1.6 GB `search.db`.  The script skips nodes with no name / address / place
tag, resulting in ~6 M indexed features.

### 2. Start the server

```bash
python3 app.py
# → http://localhost:5017
```

---

## API reference

### `GET /api/search`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `q` | string | — | Search query |
| `limit` | int | 10 | Max results (1–50) |
| `type` | string | — | Filter: `address` \| `poi` \| `place` \| `named` |
| `category` | string | — | Filter by OSM category (e.g. `restaurant`) |
| `lat` | float | — | User latitude (enables distance scoring) |
| `lon` | float | — | User longitude |
| `ranking` | string | `v1` | Scoring mode: `v1` or `v2` |

**Response**

```json
{
  "query": "brussels airport",
  "count": 10,
  "ranking": "v2",
  "intent": {
    "type": "park",
    "label": "Parking near",
    "icon": "🅿️",
    "anchor": { "name": "Brussels Airport", "lat": 50.8978, "lon": 4.4831, "id": "…" },
    "note": null,
    "suggestions": [{ "label": "🏨 Hotels", "q": "hotel near brussels airport" }]
  },
  "results": [ { "id": "…", "name": "…", "lat": …, "lon": …, "score": …, … } ]
}
```

### `GET /api/reverse`

| Parameter | Type | Description |
|---|---|---|
| `lat` | float | Latitude |
| `lon` | float | Longitude |
| `limit` | int | Max results (1–20, default 5) |

### `GET /api/check_reservation`

| Parameter | Type | Description |
|---|---|---|
| `url` | string | Restaurant website URL |

**Response**

```json
{
  "reservable": true,
  "platform": "TheFork",
  "source": "url",
  "reservation_url": "https://www.thefork.be/restaurant/…"
}
```

`source` is one of `url` (the site itself is a platform page), `link` (platform link found in page HTML), or `keyword` (reservation keyword found in page HTML).

---

## Index schema

```sql
CREATE TABLE features (
    id           TEXT PRIMARY KEY,   -- gers_identifier or "n/<osm_id>"
    feature_type TEXT,               -- address | poi | place | named
    name         TEXT,
    housenumber  TEXT,
    street       TEXT,
    city         TEXT,
    postcode     TEXT,
    category     TEXT,               -- OSM amenity/shop/tourism/… value
    place_type   TEXT,               -- OSM place value (city/town/village/…)
    phone        TEXT,
    website      TEXT,
    lat          REAL,
    lon          REAL,
    confidence   REAL,               -- Orbis confidence:feature (0–1)
    popularity   REAL,               -- Orbis popularity score
    search_text  TEXT                -- concatenated searchable text
);

CREATE VIRTUAL TABLE search_idx USING fts5(
    search_text,
    tokenize = "unicode61 remove_diacritics 1",
    content  = features,
    content_rowid = rowid
);
```

---

## Project structure

```
map-search/
├── app.py            Flask API + intent engine + reservation checker
├── build_index.py    PBF → SQLite FTS5 index builder
├── templates/
│   └── index.html    Leaflet UI (map + sidebar + intent banner)
└── search.db         Built index (git-ignored, ~1.6 GB for Belgium)
```

---

## Supported booking platforms

TheFork · LaFourchette · OpenTable · Quandoo · Bookatable · Zenchef · Resengo · Formitable · CoverManager · Resy · SevenRooms · Eat.be · Resto.be · Bookingkit · Planity
