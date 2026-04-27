"""
SatView Backend — FastAPI + SQLite
Run: uvicorn main:app --reload --port 8000
"""

import io, csv as csv_module, math, sqlite3, json, re, requests, uuid
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

# Unique ID for this server process lifetime.
# Deletions are tagged with this ID so each server restart starts a clean slate.
SESSION_ID = str(uuid.uuid4())

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH    = Path("satview.db")
STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)

YEARS = [2025, 2024, 2023]

EXTRA_FIELDS = [
    # Identifiers
    'point_id', 'cluster_id', 'plot_index',
    # SAM pipeline output
    'sam_status', 'sam_score', 'sam_area_m2', 'sam_mask_wkt',
    'sam_iou', 'covered_pct', 'sam_bbox_wkt', 'sam_mask_path',
    'sam_rotation_deg', 'sam_error',
    # Original plot area (from source CSV, never recomputed)
    'plot_area_m2',
    # Height data (all three years)
    'height_m_2023', 'height_class_2023', 'height_src_2023',
    'height_m_2024', 'height_class_2024', 'height_src_2024',
    'height_m_2025', 'height_class_2025', 'height_src_2025',
    # Construction status (researcher-annotated)
    'constructed_2023', 'constructed_2024', 'constructed_2025',
]
WAYBACK_BASE = "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile"
WAYBACK_RELEASES_FALLBACK = {
    2025: 13192,
    2024: 16453,
    2023: 56102,
}
_wayback_releases: dict = {}

def fetch_wayback_releases() -> dict:
    try:
        r = requests.get(
            "https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json",
            timeout=10, headers={"User-Agent": "SatView/1.0"}
        )
        if not r.ok:
            raise ValueError(f"HTTP {r.status_code}")
        config = r.json()
        by_year: dict = {}
        for rnum_str, info in config.items():
            m = re.match(r"Wayback (\d{4})-(\d{2}-\d{2})", info.get("itemTitle", ""))
            if not m:
                continue
            year = int(m.group(1))
            if year not in WAYBACK_RELEASES_FALLBACK:
                continue
            n = int(rnum_str)
            if year not in by_year or n > by_year[year]["release"]:
                by_year[year] = {
                    "release": n,
                    "label": f"{m.group(1)}-{m.group(2)}",
                }
        for y in YEARS:
            if y not in by_year:
                by_year[y] = {"release": WAYBACK_RELEASES_FALLBACK[y], "label": str(y)}
        return by_year
    except Exception as e:
        print(f"⚠ Wayback config fetch failed ({e}), using fallback")
        return {y: {"release": r, "label": str(y)} for y, r in WAYBACK_RELEASES_FALLBACK.items()}

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(houses)").fetchall()]
        if not cols:
            # Fresh database
            conn.executescript("""
            CREATE TABLE houses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT,
                coords_json TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );
            """)
            cols = [row[1] for row in conn.execute("PRAGMA table_info(houses)").fetchall()]
        elif 'coords_json' not in cols and 'lat1' in cols:
            # Migrate from old 4-column schema
            conn.execute("ALTER TABLE houses ADD COLUMN coords_json TEXT")
            rows = conn.execute(
                "SELECT id,lat1,lon1,lat2,lon2,lat3,lon3,lat4,lon4 FROM houses"
            ).fetchall()
            for row in rows:
                coords = [
                    [row[1], row[2]], [row[3], row[4]],
                    [row[5], row[6]], [row[7], row[8]]
                ]
                conn.execute(
                    "UPDATE houses SET coords_json=? WHERE id=?",
                    (json.dumps(coords), row[0])
                )
            print(f"✓ Migrated {len(rows)} rows to coords_json schema")
            cols = [row[1] for row in conn.execute("PRAGMA table_info(houses)").fetchall()]
        # Add any missing extra columns
        col_types = {
            # Identifiers
            'point_id':'TEXT', 'cluster_id':'TEXT', 'plot_index':'INTEGER',
            # SAM pipeline
            'sam_status':'TEXT', 'sam_score':'REAL', 'sam_area_m2':'REAL',
            'sam_mask_wkt':'TEXT', 'sam_iou':'REAL', 'covered_pct':'REAL',
            'sam_bbox_wkt':'TEXT', 'sam_mask_path':'TEXT',
            'sam_rotation_deg':'REAL', 'sam_error':'TEXT',
            'plot_area_m2':'REAL',
            # Height (all years)
            'height_m_2023':'REAL', 'height_class_2023':'TEXT', 'height_src_2023':'TEXT',
            'height_m_2024':'REAL', 'height_class_2024':'TEXT', 'height_src_2024':'TEXT',
            'height_m_2025':'REAL', 'height_class_2025':'TEXT', 'height_src_2025':'TEXT',
            # Construction status
            'constructed_2023':'TEXT', 'constructed_2024':'TEXT', 'constructed_2025':'TEXT',
            # Traceability — write-once originals + edit flags
            'polygon_wkt_original':'TEXT',
            'sam_mask_wkt_original':'TEXT',
            'boundary_edited':'INTEGER',
            'mask_edited':'INTEGER',
        }
        for col, typ in col_types.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE houses ADD COLUMN {col} {typ}")

        # Deletion audit log — persists snapshots of every individually-deleted house
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS deleted_houses (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                original_id    INTEGER NOT NULL,
                snapshot_json  TEXT    NOT NULL,
                deleted_at     TEXT    DEFAULT (datetime('now')),
                deleted_reason TEXT    DEFAULT 'manual',
                session_id     TEXT    DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_dh_deleted_at
                ON deleted_houses(deleted_at);
        """)
        # Migration: add session_id column if it doesn't exist yet (table pre-dates this column)
        dh_cols = [row[1] for row in conn.execute("PRAGMA table_info(deleted_houses)").fetchall()]
        if 'session_id' not in dh_cols:
            conn.execute("ALTER TABLE deleted_houses ADD COLUMN session_id TEXT DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dh_session ON deleted_houses(session_id)"
        )
    print("✓ Database initialised:", DB_PATH)

def init_dedupe_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dedupe_candidates (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                pin            TEXT    NOT NULL,
                match_rank     INTEGER NOT NULL DEFAULT 0,
                candidate_json TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_dc_pin  ON dedupe_candidates(pin);
            CREATE INDEX IF NOT EXISTS idx_dc_rank ON dedupe_candidates(pin, match_rank);

            CREATE TABLE IF NOT EXISTS dedupe_selections (
                pin           TEXT    PRIMARY KEY,
                selected_rank INTEGER NOT NULL DEFAULT 1,
                status        TEXT    NOT NULL DEFAULT 'needs_review',
                notes         TEXT,
                updated_at    TEXT    DEFAULT (datetime('now'))
            );
        """)
    print("✓ Dedupe tables ready")

# ── WKT helpers ───────────────────────────────────────────────────────────────
def parse_wkt_polygon(wkt: str) -> list:
    """Parse WKT POLYGON string → [[lat,lon], ...] (closing duplicate dropped)."""
    m = re.search(r'POLYGON\s*\(\((.+)\)\)', wkt.strip(), re.IGNORECASE)
    if not m:
        raise ValueError("Not a valid WKT POLYGON")
    pairs = []
    for pair in m.group(1).split(','):
        parts = pair.strip().split()
        if len(parts) < 2:
            continue
        lon, lat = float(parts[0]), float(parts[1])
        pairs.append([lat, lon])
    # Drop closing duplicate point
    if len(pairs) > 1 and pairs[0] == pairs[-1]:
        pairs = pairs[:-1]
    if len(pairs) < 3:
        raise ValueError("Polygon must have at least 3 unique points")
    return pairs

def coords_to_wkt(coords: list) -> str:
    """Convert [[lat,lon], ...] → WKT POLYGON string."""
    pts = coords + [coords[0]]  # close the ring
    inner = ", ".join(f"{c[1]:.10f} {c[0]:.10f}" for c in pts)
    return f"POLYGON (({inner}))"

# ── App ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_dedupe_db()
    global _wayback_releases
    _wayback_releases = fetch_wayback_releases()
    print("✓ Wayback releases:", {y: v["release"] for y, v in _wayback_releases.items()})
    yield

app = FastAPI(title="SatView API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Pydantic models ───────────────────────────────────────────────────────────
class HouseIn(BaseModel):
    label:  Optional[str] = None
    coords: list[list[float]]   # [[lat,lon], ...] — any number of points

class HouseUpdate(BaseModel):
    label:  Optional[str]               = None
    coords: Optional[list[list[float]]] = None

# ── Helpers ───────────────────────────────────────────────────────────────────
def row_to_dict(r) -> dict:
    d = dict(r)
    d['coords'] = json.loads(d.get('coords_json') or '[]')
    for f in EXTRA_FIELDS:
        d.setdefault(f, None)
    return d

def polygon_centroid(coords: list) -> tuple:
    n = len(coords)
    return sum(c[0] for c in coords) / n, sum(c[1] for c in coords) / n

# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "db": str(DB_PATH)}

@app.get("/api/wayback-releases")
def wayback_releases():
    return _wayback_releases

@app.get("/api/tiles/{year}/{z}/{y}/{x}")
def proxy_tile(year: int, z: int, y: int, x: int):
    """Proxy Wayback tiles through the backend to avoid browser CORS restrictions."""
    release = _wayback_releases.get(year, {}).get("release")
    if not release:
        raise HTTPException(404, f"No release found for year {year}")
    url = f"{WAYBACK_BASE}/{release}/{z}/{y}/{x}"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "SatView/1.0"})
        if r.status_code != 200:
            raise HTTPException(r.status_code, "Tile not found")
        return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))
    except requests.RequestException as e:
        raise HTTPException(502, f"Tile fetch failed: {e}")

# ── Houses CRUD ───────────────────────────────────────────────────────────────
@app.get("/api/houses")
def list_houses(
    page: int = Query(0, ge=0),
    page_size: int = Query(0, ge=0),
):
    """List houses. page_size=0 returns all records (default). page_size>0 paginates."""
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM houses").fetchone()[0]
        if page_size > 0:
            offset = page * page_size
            rows = conn.execute(
                "SELECT * FROM houses ORDER BY id LIMIT ? OFFSET ?",
                (page_size, offset)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM houses ORDER BY id").fetchall()
    items = [row_to_dict(r) for r in rows]
    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "has_more":  page_size > 0 and (page * page_size + page_size) < total,
        "items":     items,
    }

@app.get("/api/houses/{house_id}")
def get_house(house_id: int):
    with get_db() as conn:
        h = conn.execute("SELECT * FROM houses WHERE id=?", (house_id,)).fetchone()
        if not h:
            raise HTTPException(404, "House not found")
    return row_to_dict(h)

@app.post("/api/houses", status_code=201)
def create_house(body: HouseIn):
    if len(body.coords) < 3:
        raise HTTPException(400, "At least 3 coordinate pairs required")
    label = body.label or f"House {datetime.now().strftime('%Y%m%d-%H%M%S')}"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO houses (label, coords_json) VALUES (?, ?)",
            (label, json.dumps(body.coords))
        )
        house_id = cur.lastrowid
    return {"id": house_id, "label": label, "message": "House saved."}

@app.put("/api/houses/{house_id}")
def update_house(house_id: int, body: HouseUpdate):
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM houses WHERE id=?", (house_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "House not found")
        updates = {"updated_at": datetime.now().isoformat()}
        if body.label is not None:
            updates["label"] = body.label
        if body.coords is not None:
            updates["coords_json"] = json.dumps(body.coords)
            updates["boundary_edited"] = 1
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE houses SET {set_clause} WHERE id=?",
                     (*updates.values(), house_id))
    return {"id": house_id, "message": "Updated."}

@app.delete("/api/houses/{house_id}")
def delete_house(house_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM houses WHERE id=?", (house_id,)).fetchone()
        if row:
            conn.execute(
                "INSERT INTO deleted_houses (original_id, snapshot_json, deleted_reason, session_id) "
                "VALUES (?, ?, 'manual', ?)",
                (house_id, json.dumps(dict(row)), SESSION_ID)
            )
        conn.execute("DELETE FROM houses WHERE id=?", (house_id,))
    return {"ok": True, "message": "Deleted"}


@app.get("/api/deleted/count")
def deleted_count():
    with get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM deleted_houses WHERE session_id=?", (SESSION_ID,)
        ).fetchone()[0]
    return {"count": n}


@app.delete("/api/deleted")
def clear_deleted_log():
    with get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM deleted_houses WHERE session_id=?", (SESSION_ID,)
        ).fetchone()[0]
        conn.execute("DELETE FROM deleted_houses WHERE session_id=?", (SESSION_ID,))
    return {"cleared": n, "message": f"Deletion log cleared — {n} records removed."}

EDITABLE_META = {
    'constructed_2023', 'constructed_2024', 'constructed_2025',
    'height_class_2023', 'height_class_2024', 'height_class_2025',
    'sam_area_m2', 'sam_mask_wkt',
}

class MetaUpdate(BaseModel):
    field: str
    value: Optional[str] = None

@app.patch("/api/houses/{house_id}/meta")
def update_meta(house_id: int, body: MetaUpdate):
    if body.field not in EDITABLE_META:
        raise HTTPException(400, f"Field '{body.field}' is not editable via this endpoint")
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM houses WHERE id=?", (house_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "House not found")
        conn.execute(
            f"UPDATE houses SET {body.field}=?, updated_at=datetime('now') WHERE id=?",
            (body.value, house_id)
        )
        # Track that the mask was manually edited (for research traceability)
        if body.field == 'sam_mask_wkt':
            conn.execute("UPDATE houses SET mask_edited=1 WHERE id=?", (house_id,))
    return {"ok": True}

# ── Duplicate helpers ─────────────────────────────────────────────────────────
COORD_TOLERANCE = 0.0001   # ~11 metres

def coords_match(coords_a: list, db_row: dict) -> bool:
    coords_b = json.loads(db_row.get('coords_json') or '[]')
    if not coords_b:
        return False
    ca = polygon_centroid(coords_a)
    cb = polygon_centroid(coords_b)
    return abs(ca[0] - cb[0]) < COORD_TOLERANCE and abs(ca[1] - cb[1]) < COORD_TOLERANCE

def center_dist_m(coords_a: list, db_row: dict) -> float:
    coords_b = json.loads(db_row.get('coords_json') or '[]')
    ca = polygon_centroid(coords_a)
    cb = polygon_centroid(coords_b) if coords_b else (0.0, 0.0)
    dlat = (ca[0] - cb[0]) * 111320
    dlon = (ca[1] - cb[1]) * 111320 * math.cos(math.radians(ca[0]))
    return math.sqrt(dlat**2 + dlon**2)

# ── CSV Pre-check ─────────────────────────────────────────────────────────────
def _safe_float(v):
    try: return float(v) if v and str(v).strip() else None
    except: return None

def _safe_int(v):
    try: return int(v) if v and str(v).strip() else None
    except: return None

def _safe_constructed(v):
    """Only accept the two canonical status strings; everything else → None."""
    val = (v or '').strip().lower()
    return val if val in ('constructed', 'non_constructed') else None

def _parse_csv_rows(content: str):
    """Parse CSV content. Returns (list_of_csv_entries, list_of_error_strings)."""
    new_rows, parse_errs = [], []
    reader = csv_module.DictReader(io.StringIO(content))
    fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]

    is_new_format = 'polygon_wkt' in fieldnames and (
        'point_id' in fieldnames or 'height_m_2023' in fieldnames
    )

    if is_new_format:
        # Re-read with normalised header
        reader = csv_module.DictReader(
            io.StringIO(content),
            fieldnames=[f.strip() for f in (reader.fieldnames or [])],
            restkey='__extra__'
        )
        next(reader)   # skip original header row
        for i, row_dict in enumerate(reader):
            wkt = row_dict.get('polygon_wkt', '').strip().strip('"')
            try:
                coords = parse_wkt_polygon(wkt)
            except Exception as e:
                parse_errs.append(f"Row {i+1}: {e}")
                continue
            point_id   = row_dict.get('point_id', '').strip()
            plot_index = row_dict.get('plot_index', '').strip()
            # Prefer stored label (round-trip), fall back to computed
            csv_label  = row_dict.get('label', '').strip()
            label = csv_label or (f"{point_id} #{plot_index}" if point_id else f"House {i+1}")
            extra = {
                'point_id':          point_id,
                'cluster_id':        row_dict.get('cluster_id', '').strip(),
                'plot_index':        _safe_int(plot_index),
                # SAM pipeline
                'sam_status':        row_dict.get('sam_status', '').strip() or None,
                'sam_score':         _safe_float(row_dict.get('sam_score')),
                'sam_area_m2':       _safe_float(row_dict.get('sam_area_m2')),
                'sam_mask_wkt':      row_dict.get('sam_mask_wkt', '').strip() or None,
                'sam_iou':           _safe_float(row_dict.get('sam_iou')),
                'covered_pct':       _safe_float(row_dict.get('covered_pct')),
                'sam_bbox_wkt':      row_dict.get('sam_bbox_wkt', '').strip() or None,
                'sam_mask_path':     row_dict.get('sam_mask_path', '').strip() or None,
                'sam_rotation_deg':  _safe_float(row_dict.get('sam_rotation_deg')),
                'sam_error':         row_dict.get('sam_error', '').strip() or None,
                'plot_area_m2':      _safe_float(row_dict.get('plot_area_m2')),
                # Heights
                'height_m_2023':     _safe_float(row_dict.get('height_m_2023')),
                'height_class_2023': row_dict.get('height_class_2023', '').strip() or None,
                'height_src_2023':   row_dict.get('height_src_2023', '').strip() or None,
                'height_m_2024':     _safe_float(row_dict.get('height_m_2024')),
                'height_class_2024': row_dict.get('height_class_2024', '').strip() or None,
                'height_src_2024':   row_dict.get('height_src_2024', '').strip() or None,
                'height_m_2025':     _safe_float(row_dict.get('height_m_2025')),
                'height_class_2025': row_dict.get('height_class_2025', '').strip() or None,
                'height_src_2025':   row_dict.get('height_src_2025', '').strip() or None,
                # Construction status
                'constructed_2023':  _safe_constructed(row_dict.get('constructed_2023')),
                'constructed_2024':  _safe_constructed(row_dict.get('constructed_2024')),
                'constructed_2025':  _safe_constructed(row_dict.get('constructed_2025')),
            }
            new_rows.append({"row_index": i, "label": label, "coords": coords, **extra})
    else:
        # Old format: plain WKT rows or lat/lon rows
        lines = [l.strip() for l in content.strip().splitlines()
                 if l.strip() and not l.startswith("#")]
        if lines and 'polygon_wkt' in lines[0].lower():
            lines = lines[1:]
        for i, line in enumerate(lines):
            raw = line.strip().strip('"')
            try:
                if 'POLYGON' in raw.upper():
                    coords = parse_wkt_polygon(raw)
                else:
                    vals = [v.strip() for v in raw.split(",")]
                    if len(vals) < 8:
                        raise ValueError(f"need 8 values, got {len(vals)}")
                    nums = [float(v) for v in vals[:8]]
                    coords = [[nums[0],nums[1]],[nums[2],nums[3]],
                              [nums[4],nums[5]],[nums[6],nums[7]]]
            except Exception as e:
                parse_errs.append(f"Row {i+1}: {e}")
                continue
            new_rows.append({"row_index": i, "label": f"House {i+1}", "coords": coords})

    return new_rows, parse_errs

@app.post("/api/import/check")
async def check_csv(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8")
    parsed_rows, parse_errs = _parse_csv_rows(content)

    with get_db() as conn:
        db_rows = conn.execute("SELECT * FROM houses ORDER BY id").fetchall()
    db_rows = [dict(r) for r in db_rows]

    new_rows, conflicts = [], []
    for entry in parsed_rows:
        coords = entry["coords"]
        match = next((r for r in db_rows if coords_match(coords, r)), None)
        if match:
            conflicts.append({
                "csv":    entry,
                "db":     {**dict(match), "coords": json.loads(match.get('coords_json') or '[]')},
                "dist_m": round(center_dist_m(coords, match), 2)
            })
        else:
            new_rows.append(entry)

    return {
        "new_count":      len(new_rows),
        "conflict_count": len(conflicts),
        "new_rows":       new_rows,
        "conflicts":      conflicts,
        "parse_errors":   parse_errs,
    }

# ── CSV Confirmed Import ──────────────────────────────────────────────────────
class ConflictResolution(BaseModel):
    action:  str   # "keep_db" | "keep_csv" | "keep_both"
    db_id:   int
    csv_row: dict

class ConfirmedImport(BaseModel):
    new_rows:    list[dict]
    resolutions: list[ConflictResolution]

def _insert_house(label: str, coords: list, extra: dict = None) -> int:
    extra = extra or {}
    # polygon_wkt_original / sam_mask_wkt_original are set ONCE at insert time
    # and must never be overwritten — they preserve the as-imported state for research.
    cols = (
        ['label', 'coords_json', 'polygon_wkt_original', 'sam_mask_wkt_original']
        + EXTRA_FIELDS
    )
    vals = (
        [label, json.dumps(coords),
         coords_to_wkt(coords),
         extra.get('sam_mask_wkt') or '']
        + [extra.get(f) for f in EXTRA_FIELDS]
    )
    col_str      = ', '.join(cols)
    placeholders = ', '.join('?' * len(vals))
    with get_db() as conn:
        cur = conn.execute(f"INSERT INTO houses ({col_str}) VALUES ({placeholders})", vals)
        return cur.lastrowid

def _update_house_from_csv(db_id: int, csv_row: dict):
    coords = csv_row["coords"]
    label  = csv_row.get("label")
    # boundary_edited=1 because we're accepting the CSV's (possibly edited) boundary.
    # polygon_wkt_original and sam_mask_wkt_original are intentionally excluded —
    # they are write-once at INSERT and must never be overwritten.
    set_parts = ["label=?", "coords_json=?", "boundary_edited=1", "updated_at=datetime('now')"]
    vals = [label, json.dumps(coords)]
    for f in EXTRA_FIELDS:
        if f in csv_row:
            set_parts.append(f"{f}=?")
            vals.append(csv_row[f])
    vals.append(db_id)
    with get_db() as conn:
        conn.execute(f"UPDATE houses SET {', '.join(set_parts)} WHERE id=?", vals)

@app.post("/api/import/confirmed")
def confirmed_import(body: ConfirmedImport):
    inserted = []
    updated  = []

    for row in body.new_rows:
        hid = _insert_house(row.get("label", "House"), row["coords"], row)
        inserted.append(hid)

    for res in body.resolutions:
        if res.action == "keep_db":
            pass
        elif res.action == "keep_csv":
            _update_house_from_csv(res.db_id, res.csv_row)
            updated.append(res.db_id)
        elif res.action == "keep_both":
            csv_row = dict(res.csv_row)
            csv_row["label"] = csv_row.get("label", "House") + " (copy)"
            hid = _insert_house(csv_row["label"], csv_row["coords"], csv_row)
            inserted.append(hid)

    return {
        "inserted": len(inserted),
        "updated":  len(updated),
        "ids":      inserted,
        "message":  f"✓ {len(inserted)} inserted, {len(updated)} updated."
    }

# ── DB utilities ─────────────────────────────────────────────────────────────
@app.delete("/api/db/clear")
def clear_database():
    """Delete every house record. Irreversible."""
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM houses").fetchone()[0]
        conn.execute("DELETE FROM houses")
        # Reset auto-increment counter so new IDs start from 1 again
        conn.execute("DELETE FROM sqlite_sequence WHERE name='houses'")
    print(f"✓ Cleared {count} records from database")
    return {"deleted": count, "message": f"Database cleared — {count} records removed."}

# ── CSV Export ────────────────────────────────────────────────────────────────
@app.get("/api/export/csv")
def export_csv():
    with get_db() as conn:
        active_rows  = conn.execute("SELECT * FROM houses ORDER BY id").fetchall()
        deleted_rows = conn.execute(
            "SELECT original_id, snapshot_json, deleted_at, deleted_reason "
            "FROM deleted_houses WHERE session_id=? ORDER BY deleted_at",
            (SESSION_ID,)
        ).fetchall()

    # Column order: identifiers → boundary → mask → height → status → SAM → audit → tracking
    export_cols = [
        # ── Identifiers ──────────────────────────────────────────
        'id', 'label', 'point_id', 'cluster_id', 'plot_index',
        # ── Boundary traceability ────────────────────────────────
        'polygon_wkt_original',   # as-imported boundary (write-once, never edited)
        'polygon_wkt',            # current boundary (may have been manually adjusted)
        'boundary_edited',        # 1 = researcher moved boundary points
        'sam_area_m2',            # area of the CURRENT boundary (m²)
        'plot_area_m2',           # area of the ORIGINAL plot boundary from source (m²)
        # ── Mask traceability ────────────────────────────────────
        'sam_mask_wkt_original',  # as-imported SAM mask (write-once)
        'sam_mask_wkt',           # current mask (may have been manually adjusted)
        'mask_edited',            # 1 = researcher moved mask points
        # ── Height (all years) ───────────────────────────────────
        'height_m_2023', 'height_class_2023', 'height_src_2023',
        'height_m_2024', 'height_class_2024', 'height_src_2024',
        'height_m_2025', 'height_class_2025', 'height_src_2025',
        # ── Construction status (researcher-annotated) ───────────
        'constructed_2023', 'constructed_2024', 'constructed_2025',
        # ── SAM pipeline metadata ────────────────────────────────
        'sam_status', 'sam_score', 'sam_iou', 'covered_pct',
        'sam_bbox_wkt', 'sam_mask_path', 'sam_rotation_deg', 'sam_error',
        # ── Audit timestamps ─────────────────────────────────────
        'created_at', 'updated_at',
        # ── Deletion tracking (empty for active records) ─────────
        '_record_status', 'deleted_at', 'deleted_reason',
    ]

    output = io.StringIO()
    writer = csv_module.writer(output, quoting=csv_module.QUOTE_MINIMAL)
    writer.writerow(export_cols)

    db_cols: set = set(active_rows[0].keys()) if active_rows else set()

    # ── Active records ────────────────────────────────────────────────────────
    for r in active_rows:
        coords = json.loads(r['coords_json'] or '[]')
        if not coords:
            continue
        current_wkt = coords_to_wkt(coords)
        row_data = []
        for col in export_cols:
            if col == 'polygon_wkt':
                row_data.append(current_wkt)
            elif col in ('_record_status',):
                row_data.append('active')
            elif col in ('deleted_at', 'deleted_reason'):
                row_data.append('')
            elif col not in db_cols:
                row_data.append('')
            else:
                v = r[col]
                row_data.append(v if v is not None else '')
        writer.writerow(row_data)

    # ── Deleted records (appended after active, clearly marked) ───────────────
    for dr in deleted_rows:
        try:
            snap = json.loads(dr['snapshot_json'])
        except (json.JSONDecodeError, TypeError):
            continue
        coords = json.loads(snap.get('coords_json') or '[]')
        current_wkt = coords_to_wkt(coords) if coords else ''
        snap_cols = set(snap.keys())
        row_data = []
        for col in export_cols:
            if col == 'id':
                row_data.append(snap.get('id', dr['original_id']))
            elif col == 'polygon_wkt':
                row_data.append(current_wkt)
            elif col == '_record_status':
                row_data.append('deleted')
            elif col == 'deleted_at':
                row_data.append(dr['deleted_at'] or '')
            elif col == 'deleted_reason':
                row_data.append(dr['deleted_reason'] or '')
            elif col in snap_cols:
                v = snap.get(col)
                row_data.append(v if v is not None else '')
            else:
                row_data.append('')
        writer.writerow(row_data)

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=satview_export.csv"}
    )

# ── Deduplicate Pipeline ──────────────────────────────────────────────────────

class DedupeSelectBody(BaseModel):
    rank:  int
    notes: Optional[str] = None

class BulkConfirmBody(BaseModel):
    min_score: float = 0.70


@app.post("/api/dedupe/upload")
async def dedupe_upload(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8-sig")
    reader  = csv_module.DictReader(io.StringIO(content))
    all_rows = list(reader)
    if not all_rows:
        raise HTTPException(400, "Empty file")

    missing = {'pin', '_match_rank', 'polygon_wkt'} - set(all_rows[0].keys())
    if missing:
        raise HTTPException(400, f"Missing required columns: {missing}")

    from collections import defaultdict
    by_pin: dict = defaultdict(list)
    for row in all_rows:
        pin = row.get('pin', '').strip()
        if pin:
            by_pin[pin].append(row)

    ins_cands, ins_sels = [], []
    for pin, candidates in by_pin.items():
        candidates.sort(key=lambda r: int(r.get('_match_rank') or 999))
        for c in candidates:
            ins_cands.append((pin, int(c.get('_match_rank') or 0), json.dumps(c)))
        rank1  = next((c for c in candidates if str(c.get('_match_rank', '')).strip() == '1'), candidates[0])
        score  = _safe_float(rank1.get('sam_score')) or 0.0
        status = 'auto' if score >= 0.70 else 'needs_review'
        ins_sels.append((pin, 1, status))

    with get_db() as conn:
        conn.execute("DELETE FROM dedupe_candidates")
        conn.execute("DELETE FROM dedupe_selections")
        conn.executemany(
            "INSERT INTO dedupe_candidates (pin, match_rank, candidate_json) VALUES (?,?,?)",
            ins_cands
        )
        conn.executemany(
            "INSERT INTO dedupe_selections (pin, selected_rank, status) VALUES (?,?,?)",
            ins_sels
        )

    total      = len(by_pin)
    auto_cnt   = sum(1 for *_, s in ins_sels if s == 'auto')
    review_cnt = total - auto_cnt
    return {
        "total_pins":    total,
        "auto_selected": auto_cnt,
        "needs_review":  review_cnt,
        "total_rows":    len(all_rows),
        "message": f"Loaded {total} PINs ({len(all_rows)} rows). {auto_cnt} auto-selected (score≥0.70), {review_cnt} need manual review."
    }


@app.get("/api/dedupe/progress")
def dedupe_progress():
    with get_db() as conn:
        total     = conn.execute("SELECT COUNT(DISTINCT pin) FROM dedupe_candidates").fetchone()[0]
        confirmed = conn.execute("SELECT COUNT(*) FROM dedupe_selections WHERE status='confirmed'").fetchone()[0]
        auto      = conn.execute("SELECT COUNT(*) FROM dedupe_selections WHERE status='auto'").fetchone()[0]
        review    = conn.execute("SELECT COUNT(*) FROM dedupe_selections WHERE status='needs_review'").fetchone()[0]
    return {"total": total, "confirmed": confirmed, "auto": auto, "needs_review": review}


@app.get("/api/dedupe/pins")
def dedupe_pins(
    page:      int = Query(0, ge=0),
    page_size: int = Query(60, ge=1, le=200),
    filter:    str = Query("all"),
):
    where  = "" if filter == "all" else "WHERE s.status=?"
    params: list = [] if filter == "all" else [filter]

    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM dedupe_selections s {where}", params).fetchone()[0]
        rows  = conn.execute(f"""
            SELECT s.pin, s.selected_rank, s.status, s.notes,
                   (SELECT c.candidate_json FROM dedupe_candidates c
                    WHERE c.pin=s.pin AND c.match_rank=s.selected_rank LIMIT 1) AS sel_json,
                   (SELECT COUNT(*) FROM dedupe_candidates c WHERE c.pin=s.pin) AS cand_count
            FROM dedupe_selections s
            {where}
            ORDER BY
                CASE s.status WHEN 'needs_review' THEN 0 WHEN 'auto' THEN 1 ELSE 2 END,
                s.pin
            LIMIT ? OFFSET ?
        """, params + [page_size, page * page_size]).fetchall()

    items = []
    for r in rows:
        sel = json.loads(r['sel_json']) if r['sel_json'] else {}
        items.append({
            "pin":           r['pin'],
            "selected_rank": r['selected_rank'],
            "status":        r['status'],
            "notes":         r['notes'],
            "cand_count":    r['cand_count'],
            "sam_score":     _safe_float(sel.get('sam_score')),
            "circle_name":   sel.get('circle_name', ''),
            "locality_name": sel.get('locality_name', ''),
        })
    return {
        "total":    total,
        "page":     page,
        "page_size": page_size,
        "has_more": (page + 1) * page_size < total,
        "items":    items,
    }


@app.get("/api/dedupe/pin/{pin:path}")
def dedupe_pin_detail(pin: str):
    with get_db() as conn:
        cands = conn.execute(
            "SELECT match_rank, candidate_json FROM dedupe_candidates WHERE pin=? ORDER BY match_rank",
            (pin,)
        ).fetchall()
        sel = conn.execute(
            "SELECT selected_rank, status, notes FROM dedupe_selections WHERE pin=?",
            (pin,)
        ).fetchone()
    if not cands:
        raise HTTPException(404, "PIN not found in dedupe session")
    return {
        "pin":           pin,
        "candidates":    [{"rank": r['match_rank'], **json.loads(r['candidate_json'])} for r in cands],
        "selected_rank": sel['selected_rank'] if sel else 1,
        "status":        sel['status']        if sel else 'needs_review',
        "notes":         sel['notes']         if sel else '',
    }


@app.post("/api/dedupe/pin/{pin:path}/select")
def dedupe_select(pin: str, body: DedupeSelectBody):
    with get_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM dedupe_candidates WHERE pin=? AND match_rank=?", (pin, body.rank)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"Rank {body.rank} not found for PIN {pin}")
        conn.execute("""
            INSERT INTO dedupe_selections (pin, selected_rank, status, notes, updated_at)
            VALUES (?, ?, 'confirmed', ?, datetime('now'))
            ON CONFLICT(pin) DO UPDATE SET
                selected_rank = excluded.selected_rank,
                status        = 'confirmed',
                notes         = excluded.notes,
                updated_at    = excluded.updated_at
        """, (pin, body.rank, body.notes or ''))
    return {"ok": True}


@app.post("/api/dedupe/bulk-confirm")
def dedupe_bulk_confirm(body: BulkConfirmBody):
    with get_db() as conn:
        auto_rows = conn.execute(
            "SELECT pin, selected_rank FROM dedupe_selections WHERE status='auto'"
        ).fetchall()
        confirmed = 0
        for row in auto_rows:
            cand = conn.execute(
                "SELECT candidate_json FROM dedupe_candidates WHERE pin=? AND match_rank=?",
                (row['pin'], row['selected_rank'])
            ).fetchone()
            if not cand:
                continue
            score = _safe_float(json.loads(cand['candidate_json']).get('sam_score')) or 0.0
            if score >= body.min_score:
                conn.execute(
                    "UPDATE dedupe_selections SET status='confirmed', updated_at=datetime('now') WHERE pin=?",
                    (row['pin'],)
                )
                confirmed += 1
    return {"confirmed": confirmed}


@app.get("/api/dedupe/export")
def dedupe_export():
    with get_db() as conn:
        sels = conn.execute(
            "SELECT pin, selected_rank, status, notes FROM dedupe_selections"
        ).fetchall()
    if not sels:
        raise HTTPException(400, "No dedupe session loaded. Upload a CSV first.")

    PIN_COLS = [
        'pin', 'circle_name', 'locality_name', 'circle_arm', 'pool_type',
        'in_tax_admin', 'tax_admin_arm', 'in_citizen_survey', 'survey_group',
        'organized_type', 'is_gated', 'is_planned', 'is_organized',
        'garv', 'total_property_tax', 'total_covered_area', 'total_land_area',
        'latitude', 'longitude', '_match_type', '_distance_m', '_source_file',
    ]
    PLOT_COLS = [
        'point_id', 'plot_index', 'polygon_wkt', 'plot_area_m2',
        'sam_status', 'sam_score', 'sam_iou', 'sam_area_m2', 'covered_pct',
        'sam_bbox_wkt', 'sam_mask_wkt', 'sam_mask_path', 'sam_rotation_deg', 'sam_error',
        'height_m_2023', 'height_class_2023', 'height_src_2023',
        'height_m_2024', 'height_class_2024', 'height_src_2024',
        'height_m_2025', 'height_class_2025', 'height_src_2025',
    ]
    META_COLS = ['_selected_rank', '_verified', '_dedupe_notes']
    ALL_COLS  = PIN_COLS + PLOT_COLS + META_COLS

    output = io.StringIO()
    writer = csv_module.writer(output, quoting=csv_module.QUOTE_MINIMAL)
    writer.writerow(ALL_COLS)

    with get_db() as conn:
        for sel in sels:
            cand = conn.execute(
                "SELECT candidate_json FROM dedupe_candidates WHERE pin=? AND match_rank=?",
                (sel['pin'], sel['selected_rank'])
            ).fetchone()
            if not cand:
                continue
            data = json.loads(cand['candidate_json'])
            row  = []
            for col in ALL_COLS:
                if   col == '_selected_rank': row.append(sel['selected_rank'])
                elif col == '_verified':      row.append(1 if sel['status'] == 'confirmed' else 0)
                elif col == '_dedupe_notes':  row.append(sel['notes'] or '')
                else:                         row.append(data.get(col, ''))
            writer.writerow(row)

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=modeltown_deduplicated.csv"}
    )


@app.delete("/api/dedupe/session")
def dedupe_reset():
    with get_db() as conn:
        conn.execute("DELETE FROM dedupe_candidates")
        conn.execute("DELETE FROM dedupe_selections")
    return {"message": "Dedupe session cleared"}


# ── Serve frontend ─────────────────────────────────────────────────────────────
@app.get("/")
def serve_frontend():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"message": "Place index.html in the static/ folder"}
