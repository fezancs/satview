"""
SatView Backend — FastAPI + SQLite
Run: uvicorn main:app --reload --port 8000
"""

import io, math, sqlite3, json, re, requests
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH    = Path("satview.db")
STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)

YEARS = [2025, 2024, 2023]
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
    print("✓ Database initialised:", DB_PATH)

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
def list_houses():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM houses ORDER BY id").fetchall()
    return [row_to_dict(r) for r in rows]

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
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE houses SET {set_clause} WHERE id=?",
                     (*updates.values(), house_id))
    return {"id": house_id, "message": "Updated."}

@app.delete("/api/houses/{house_id}")
def delete_house(house_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM houses WHERE id=?", (house_id,))
    return {"message": "Deleted"}

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
@app.post("/api/import/check")
async def check_csv(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8")
    lines   = [l.strip() for l in content.strip().splitlines()
               if l.strip() and not l.startswith("#")]

    # Skip header row
    if lines and 'polygon_wkt' in lines[0].lower():
        lines = lines[1:]

    with get_db() as conn:
        db_rows = conn.execute("SELECT * FROM houses ORDER BY id").fetchall()
    db_rows = [dict(r) for r in db_rows]

    new_rows   = []
    conflicts  = []
    parse_errs = []

    for i, line in enumerate(lines):
        raw = line.strip().strip('"')
        try:
            if 'POLYGON' in raw.upper():
                coords = parse_wkt_polygon(raw)
            else:
                # Fallback: old lat1,lon1,lat2,lon2,lat3,lon3,lat4,lon4[,label] format
                vals = [v.strip() for v in raw.split(",")]
                if len(vals) < 8:
                    raise ValueError(f"need 8 values, got {len(vals)}")
                nums = [float(v) for v in vals[:8]]
                coords = [
                    [nums[0], nums[1]], [nums[2], nums[3]],
                    [nums[4], nums[5]], [nums[6], nums[7]]
                ]
        except Exception as e:
            parse_errs.append(f"Row {i+1}: {e}")
            continue

        label = f"House {i+1}"
        csv_entry = {"row_index": i, "label": label, "coords": coords, "raw": line}

        match = next((r for r in db_rows if coords_match(coords, r)), None)
        if match:
            conflicts.append({
                "csv":    csv_entry,
                "db":     {**dict(match), "coords": json.loads(match.get('coords_json') or '[]')},
                "dist_m": round(center_dist_m(coords, match), 2)
            })
        else:
            new_rows.append(csv_entry)

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

@app.post("/api/import/confirmed")
def confirmed_import(body: ConfirmedImport):
    inserted = []
    updated  = []

    for row in body.new_rows:
        coords = row["coords"]
        label  = row.get("label", "House")
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO houses (label, coords_json) VALUES (?, ?)",
                (label, json.dumps(coords))
            )
            hid = cur.lastrowid
        inserted.append(hid)

    for res in body.resolutions:
        if res.action == "keep_db":
            pass

        elif res.action == "keep_csv":
            coords = res.csv_row["coords"]
            label  = res.csv_row.get("label")
            with get_db() as conn:
                conn.execute(
                    "UPDATE houses SET label=?, coords_json=?, updated_at=datetime('now') WHERE id=?",
                    (label, json.dumps(coords), res.db_id)
                )
            updated.append(res.db_id)

        elif res.action == "keep_both":
            coords = res.csv_row["coords"]
            label  = res.csv_row.get("label", "House (copy)")
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO houses (label, coords_json) VALUES (?, ?)",
                    (label + " (copy)", json.dumps(coords))
                )
                hid = cur.lastrowid
            inserted.append(hid)

    return {
        "inserted": len(inserted),
        "updated":  len(updated),
        "ids":      inserted,
        "message":  f"✓ {len(inserted)} inserted, {len(updated)} updated."
    }

# ── CSV Export ────────────────────────────────────────────────────────────────
@app.get("/api/export/csv")
def export_csv():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM houses ORDER BY id").fetchall()

    output = io.StringIO()
    output.write("polygon_wkt\n")
    for r in rows:
        coords = json.loads(r['coords_json'] or '[]')
        if not coords:
            continue
        output.write(f'"{coords_to_wkt(coords)}"\n')

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=satview_export.csv"}
    )

# ── Serve frontend ─────────────────────────────────────────────────────────────
@app.get("/")
def serve_frontend():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"message": "Place index.html in the static/ folder"}
