"""
SatView Backend — FastAPI + SQLite + Satellite Image Cropping
Run: uvicorn main:app --reload --port 8000
"""

import os, io, math, sqlite3, csv, json, time, requests
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

from PIL import Image, ImageDraw

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH    = Path("satview.db")
CROPS_DIR  = Path("crops")
STATIC_DIR = Path("static")          # frontend HTML lives here
CROPS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

YEARS = [2025, 2024, 2023, 2022]
WAYBACK_BASE = "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile"
WAYBACK_RELEASES = {
    2025: 13192,
    2024: 16453,
    2023: 56102,
    2022: 57659,
}

TILE_SIZE = 256   # pixels per OSM tile
CROP_ZOOM = 19    # zoom level used for cropping

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS houses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            label       TEXT,
            lat1 REAL, lon1 REAL,
            lat2 REAL, lon2 REAL,
            lat3 REAL, lon3 REAL,
            lat4 REAL, lon4 REAL,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS crops (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            house_id    INTEGER REFERENCES houses(id) ON DELETE CASCADE,
            year        INTEGER,
            file_path   TEXT,
            file_size   INTEGER,
            width       INTEGER,
            height      INTEGER,
            zoom        INTEGER,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        """)
    print("✓ Database initialised:", DB_PATH)

# ── App ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="SatView API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/crops",  StaticFiles(directory=CROPS_DIR),  name="crops")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Geo helpers ───────────────────────────────────────────────────────────────
def deg2num(lat_deg, lon_deg, zoom):
    lat_r = math.radians(lat_deg)
    n = 2 ** zoom
    x = int((lon_deg + 180) / 360 * n)
    y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
    return x, y

def deg2pixel(lat_deg, lon_deg, zoom):
    """Pixel offset within the full tile grid."""
    lat_r = math.radians(lat_deg)
    n = 2 ** zoom
    x = (lon_deg + 180) / 360 * n * TILE_SIZE
    y = (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n * TILE_SIZE
    return x, y

def fetch_tile(release: int, z: int, x: int, y: int) -> Optional[Image.Image]:
    url = f"{WAYBACK_BASE}/{release}/{z}/{y}/{x}"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "SatView/1.0"})
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as e:
        print(f"  Tile fetch error {url}: {e}")
    return None

def crop_satellite(house_id: int, coords: list[list[float]], year: int) -> dict:
    """
    Download tiles for a house boundary, stitch them, crop the bounding box,
    save to crops/<house_id>/<year>.jpg, return file info dict.
    """
    release = WAYBACK_RELEASES.get(year, WAYBACK_RELEASES[2025])
    z = CROP_ZOOM

    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    # Add a small padding (~15m)
    pad = 0.00015
    min_lat -= pad; max_lat += pad
    min_lon -= pad; max_lon += pad

    # Tile range
    tx_min, ty_min = deg2num(max_lat, min_lon, z)
    tx_max, ty_max = deg2num(min_lat, max_lon, z)

    # Clamp to reasonable range
    num_tiles = (tx_max - tx_min + 1) * (ty_max - ty_min + 1)
    if num_tiles > 25:
        print(f"  Too many tiles ({num_tiles}) at zoom {z}, falling back to {z-1}")
        z -= 1
        tx_min, ty_min = deg2num(max_lat, min_lon, z)
        tx_max, ty_max = deg2num(min_lat, max_lon, z)

    tile_cols = tx_max - tx_min + 1
    tile_rows = ty_max - ty_min + 1

    # Stitch tiles
    canvas_w = tile_cols * TILE_SIZE
    canvas_h = tile_rows * TILE_SIZE
    canvas = Image.new("RGB", (canvas_w, canvas_h), (30, 30, 30))

    print(f"  Fetching {tile_cols}×{tile_rows} tiles for house {house_id} year {year}…")
    for ty in range(ty_min, ty_max + 1):
        for tx in range(tx_min, tx_max + 1):
            tile = fetch_tile(release, z, tx, ty)
            if tile:
                px = (tx - tx_min) * TILE_SIZE
                py = (ty - ty_min) * TILE_SIZE
                canvas.paste(tile, (px, py))
            time.sleep(0.05)   # be polite to tile server

    # Pixel positions of bounding box within canvas
    def world_to_canvas(lat, lon):
        wx, wy = deg2pixel(lat, lon, z)
        cx = wx - tx_min * TILE_SIZE
        cy = wy - ty_min * TILE_SIZE
        return cx, cy

    cx_min, cy_min = world_to_canvas(max_lat, min_lon)
    cx_max, cy_max = world_to_canvas(min_lat, max_lon)
    left   = max(0, int(cx_min))
    top    = max(0, int(cy_min))
    right  = min(canvas_w, int(cx_max))
    bottom = min(canvas_h, int(cy_max))

    cropped = canvas.crop((left, top, right, bottom))

    # Draw polygon overlay
    draw = ImageDraw.Draw(cropped)
    poly_pixels = []
    for lat, lon in coords:
        px, py = world_to_canvas(lat, lon)
        poly_pixels.append((px - left, py - top))

    if len(poly_pixels) >= 3:
        draw.polygon(poly_pixels, outline=(0, 229, 255), fill=None)
        draw.polygon(poly_pixels, outline=(0, 229, 255))
        for pt in poly_pixels:
            r = 4
            draw.ellipse([pt[0]-r, pt[1]-r, pt[0]+r, pt[1]+r], fill="white", outline=(0,229,255))

    # Save
    out_dir = CROPS_DIR / str(house_id)
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{year}.jpg"
    cropped.save(out_path, "JPEG", quality=90)

    size = out_path.stat().st_size
    print(f"  ✓ Saved crop: {out_path} ({size} bytes, {cropped.width}×{cropped.height}px)")
    return {
        "file_path": str(out_path),
        "file_size": size,
        "width":     cropped.width,
        "height":    cropped.height,
        "zoom":      z,
    }

# ── Pydantic models ───────────────────────────────────────────────────────────
class HouseIn(BaseModel):
    label:  Optional[str] = None
    coords: list[list[float]]   # [[lat,lon], [lat,lon], [lat,lon], [lat,lon]]

class HouseUpdate(BaseModel):
    label:  Optional[str]           = None
    coords: Optional[list[list[float]]] = None

# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "db": str(DB_PATH), "crops_dir": str(CROPS_DIR)}

# ── Houses CRUD ───────────────────────────────────────────────────────────────
@app.get("/api/houses")
def list_houses():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT h.*,
                   COUNT(c.id) as crop_count
            FROM   houses h
            LEFT JOIN crops c ON c.house_id = h.id
            GROUP BY h.id
            ORDER BY h.id
        """).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/houses/{house_id}")
def get_house(house_id: int):
    with get_db() as conn:
        h = conn.execute("SELECT * FROM houses WHERE id=?", (house_id,)).fetchone()
        if not h:
            raise HTTPException(404, "House not found")
        crops = conn.execute("SELECT * FROM crops WHERE house_id=? ORDER BY year DESC", (house_id,)).fetchall()
    result = dict(h)
    result["crops"] = [dict(c) for c in crops]
    return result

@app.post("/api/houses", status_code=201)
def create_house(body: HouseIn, background_tasks: BackgroundTasks):
    if len(body.coords) != 4:
        raise HTTPException(400, "Exactly 4 coordinate pairs required")
    c = body.coords
    label = body.label or f"House {datetime.now().strftime('%Y%m%d-%H%M%S')}"
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO houses (label, lat1,lon1, lat2,lon2, lat3,lon3, lat4,lon4)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (label,
              c[0][0], c[0][1], c[1][0], c[1][1],
              c[2][0], c[2][1], c[3][0], c[3][1]))
        house_id = cur.lastrowid
    background_tasks.add_task(generate_crops, house_id, body.coords)
    return {"id": house_id, "label": label, "message": "House saved. Crops generating in background."}

@app.put("/api/houses/{house_id}")
def update_house(house_id: int, body: HouseUpdate, background_tasks: BackgroundTasks):
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM houses WHERE id=?", (house_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "House not found")

        updates = {"updated_at": datetime.now().isoformat()}
        if body.label is not None:
            updates["label"] = body.label
        if body.coords is not None:
            c = body.coords
            updates.update({
                "lat1": c[0][0], "lon1": c[0][1],
                "lat2": c[1][0], "lon2": c[1][1],
                "lat3": c[2][0], "lon3": c[2][1],
                "lat4": c[3][0], "lon4": c[3][1],
            })

        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE houses SET {set_clause} WHERE id=?",
                     (*updates.values(), house_id))

    if body.coords:
        # Delete old crops and regenerate
        with get_db() as conn:
            conn.execute("DELETE FROM crops WHERE house_id=?", (house_id,))
        background_tasks.add_task(generate_crops, house_id, body.coords)

    return {"id": house_id, "message": "Updated. Crops regenerating." if body.coords else "Updated."}

@app.delete("/api/houses/{house_id}")
def delete_house(house_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM houses WHERE id=?", (house_id,))
    # Remove crop files
    import shutil
    crop_folder = CROPS_DIR / str(house_id)
    if crop_folder.exists():
        shutil.rmtree(crop_folder)
    return {"message": "Deleted"}

# ── Duplicate helpers ─────────────────────────────────────────────────────────
COORD_TOLERANCE = 0.0001   # ~11 metres — coords within this are "same location"

def coords_match(a: list, b_row) -> bool:
    """True if all 4 corner pairs are within COORD_TOLERANCE of each other."""
    pairs = [('lat1','lon1'),('lat2','lon2'),('lat3','lon3'),('lat4','lon4')]
    for i, (lk, lnk) in enumerate(pairs):
        if abs(a[i][0] - b_row[lk]) > COORD_TOLERANCE:
            return False
        if abs(a[i][1] - b_row[lnk]) > COORD_TOLERANCE:
            return False
    return True

def center_dist_m(a: list, b_row) -> float:
    """Approximate distance in metres between polygon centres."""
    pairs = [('lat1','lon1'),('lat2','lon2'),('lat3','lon3'),('lat4','lon4')]
    clat_a = sum(c[0] for c in a) / 4
    clon_a = sum(c[1] for c in a) / 4
    clat_b = sum(b_row[lk] for lk,_ in pairs) / 4
    clon_b = sum(b_row[lnk] for _,lnk in pairs) / 4
    dlat = (clat_a - clat_b) * 111320
    dlon = (clon_a - clon_b) * 111320 * math.cos(math.radians(clat_a))
    return math.sqrt(dlat**2 + dlon**2)

# ── CSV Pre-check (returns duplicates without inserting) ──────────────────────
@app.post("/api/import/check")
async def check_csv(file: UploadFile = File(...)):
    """
    Parse the CSV, compare every row against existing DB records.
    Returns:
      - new_rows   : rows with no DB match  → safe to import
      - conflicts  : rows that match an existing record
    """
    content = (await file.read()).decode("utf-8")
    lines   = [l.strip() for l in content.strip().splitlines()
               if l.strip() and not l.startswith("#")]

    with get_db() as conn:
        db_rows = conn.execute("SELECT * FROM houses ORDER BY id").fetchall()
    db_rows = [dict(r) for r in db_rows]

    new_rows   = []
    conflicts  = []
    parse_errs = []

    for i, line in enumerate(lines):
        vals = [v.strip() for v in line.split(",")]
        if len(vals) < 8:
            parse_errs.append(f"Row {i+1}: need 8 values"); continue
        try:
            nums = [float(v) for v in vals[:8]]
        except ValueError:
            parse_errs.append(f"Row {i+1}: non-numeric"); continue

        coords = [[nums[0],nums[1]],[nums[2],nums[3]],[nums[4],nums[5]],[nums[6],nums[7]]]
        label  = vals[8] if len(vals) > 8 else f"House {i+1}"
        csv_entry = { "row_index": i, "label": label, "coords": coords, "raw": line }

        match = next((r for r in db_rows if coords_match(coords, r)), None)
        if match:
            conflicts.append({
                "csv":   csv_entry,
                "db":    match,
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

# ── CSV Confirmed Import (after user resolves conflicts) ──────────────────────
class ConflictResolution(BaseModel):
    action:   str          # "keep_db" | "keep_csv" | "keep_both"
    db_id:    int
    csv_row:  dict         # {label, coords}

class ConfirmedImport(BaseModel):
    new_rows:    list[dict]               # rows with no conflict → always insert
    resolutions: list[ConflictResolution] # user decisions on each conflict

@app.post("/api/import/confirmed")
def confirmed_import(body: ConfirmedImport, background_tasks: BackgroundTasks):
    inserted = []
    updated  = []

    # 1. Insert brand-new rows
    for row in body.new_rows:
        coords = row["coords"]
        label  = row.get("label", "House")
        c = coords
        with get_db() as conn:
            cur = conn.execute("""
                INSERT INTO houses (label, lat1,lon1, lat2,lon2, lat3,lon3, lat4,lon4)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (label, c[0][0],c[0][1],c[1][0],c[1][1],c[2][0],c[2][1],c[3][0],c[3][1]))
            hid = cur.lastrowid
        background_tasks.add_task(generate_crops, hid, coords)
        inserted.append(hid)

    # 2. Apply resolutions
    for res in body.resolutions:
        if res.action == "keep_db":
            pass  # do nothing — existing DB record stays unchanged

        elif res.action == "keep_csv":
            coords = res.csv_row["coords"]
            label  = res.csv_row.get("label")
            c = coords
            with get_db() as conn:
                conn.execute("""
                    UPDATE houses
                    SET label=?, lat1=?,lon1=?,lat2=?,lon2=?,lat3=?,lon3=?,lat4=?,lon4=?,
                        updated_at=datetime('now')
                    WHERE id=?
                """, (label, c[0][0],c[0][1],c[1][0],c[1][1],c[2][0],c[2][1],c[3][0],c[3][1], res.db_id))
                # delete old crops so they regenerate
                conn.execute("DELETE FROM crops WHERE house_id=?", (res.db_id,))
            background_tasks.add_task(generate_crops, res.db_id, coords)
            updated.append(res.db_id)

        elif res.action == "keep_both":
            coords = res.csv_row["coords"]
            label  = res.csv_row.get("label", "House (copy)")
            c = coords
            with get_db() as conn:
                cur = conn.execute("""
                    INSERT INTO houses (label, lat1,lon1, lat2,lon2, lat3,lon3, lat4,lon4)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (label + " (copy)", c[0][0],c[0][1],c[1][0],c[1][1],c[2][0],c[2][1],c[3][0],c[3][1]))
                hid = cur.lastrowid
            background_tasks.add_task(generate_crops, hid, coords)
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
    output.write("# SatView export — lat1,lon1,lat2,lon2,lat3,lon3,lat4,lon4,label\n")
    for r in rows:
        output.write(f"{r['lat1']},{r['lon1']},{r['lat2']},{r['lon2']},"
                     f"{r['lat3']},{r['lon3']},{r['lat4']},{r['lon4']},{r['label']}\n")

    from fastapi.responses import Response
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=satview_export.csv"}
    )

# ── Crops ─────────────────────────────────────────────────────────────────────
@app.get("/api/houses/{house_id}/crops")
def get_crops(house_id: int):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM crops WHERE house_id=? ORDER BY year DESC", (house_id,)
        ).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/houses/{house_id}/crops/regenerate")
def regenerate_crops(house_id: int, background_tasks: BackgroundTasks):
    with get_db() as conn:
        h = conn.execute("SELECT * FROM houses WHERE id=?", (house_id,)).fetchone()
        if not h:
            raise HTTPException(404)
        coords = [[h["lat1"],h["lon1"]],[h["lat2"],h["lon2"]],
                  [h["lat3"],h["lon3"]],[h["lat4"],h["lon4"]]]
        conn.execute("DELETE FROM crops WHERE house_id=?", (house_id,))

    background_tasks.add_task(generate_crops, house_id, coords)
    return {"message": "Crop regeneration started"}

# ── Background task ───────────────────────────────────────────────────────────
def generate_crops(house_id: int, coords: list):
    """Runs in background: crop all 4 years and store in DB."""
    print(f"\n→ Generating crops for house {house_id}…")
    for year in YEARS:
        try:
            info = crop_satellite(house_id, coords, year)
            with get_db() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO crops
                    (house_id, year, file_path, file_size, width, height, zoom)
                    VALUES (?,?,?,?,?,?,?)
                """, (house_id, year, info["file_path"], info["file_size"],
                      info["width"], info["height"], info["zoom"]))
        except Exception as e:
            print(f"  ✗ Crop failed for house {house_id} year {year}: {e}")
    print(f"✓ Done crops for house {house_id}")

# ── Serve frontend ─────────────────────────────────────────────────────────────
@app.get("/")
def serve_frontend():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"message": "Place index.html in the static/ folder"}
