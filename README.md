# 🛰️ SatView — Satellite Boundary Viewer

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-009688?style=flat-square&logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-3-003B57?style=flat-square&logo=sqlite&logoColor=white)
![Leaflet](https://img.shields.io/badge/Leaflet-1.9-199900?style=flat-square&logo=leaflet&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-a8ff3e?style=flat-square)

A full-stack geospatial tool for viewing, editing, and managing house boundary coordinates against **historical satellite imagery (2022–2025)** from Esri Wayback. Built for inspecting and correcting GPS coordinate data at scale.

---

## ✨ Features

| Feature | Description |
|---|---|
| 📂 **CSV Import** | Upload boundary coordinates, auto-checked for duplicates before insert |
| 🗄️ **Database Persistence** | All houses and crops stored in SQLite with full CRUD |
| ⚠️ **Duplicate Resolver** | Side-by-side conflict resolution UI — keep DB, use CSV, or keep both |
| 🛰️ **4-Year Satellite View** | Historical imagery per house for 2022–2025 via Esri Wayback |
| 🖼️ **Auto Image Cropping** | Background satellite crop generation per house per year, stored as JPEG |
| ✎ **Draggable Editing** | Drag boundary corners directly on the map to correct bad coordinates |
| 💾 **Save to DB** | Push edited coordinates back to database with one click |
| ⬇️ **CSV Export** | Download current coordinates as a clean CSV file |
| 🔄 **Offline Fallback** | Works in CSV-only mode if backend is not running |

---

## 🏗️ Architecture

```
satview/
├── main.py              ← FastAPI backend (REST API + background crop jobs)
├── requirements.txt     ← Python dependencies
├── satview.db           ← SQLite database (auto-created on first run)
├── crops/               ← Cropped satellite images (auto-created)
│   └── {house_id}/
│       ├── 2022.jpg
│       ├── 2023.jpg
│       ├── 2024.jpg
│       └── 2025.jpg
└── static/
    └── index.html       ← Single-file frontend (Leaflet + vanilla JS)
```

**Stack:** FastAPI · SQLite · Pillow · Leaflet.js · Esri Wayback tiles · Vanilla JS (no build step)

---

## 🚀 Quick Start

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/satview.git
cd satview

# 2. Install
pip install -r requirements.txt

# 3. Run
uvicorn main:app --reload --port 8000

# 4. Open http://localhost:8000
```

> Database and crop folders are created automatically on first run. No API keys required.

---

## 📋 CSV Format

```csv
lat1,lon1,lat2,lon2,lat3,lon3,lat4,lon4[,optional_label]

# Example:
31.5204,74.3587,31.5214,74.3587,31.5214,74.3597,31.5204,74.3597,House A
31.5310,74.3620,31.5320,74.3620,31.5320,74.3630,31.5310,74.3630,House B
```

- Lines starting with `#` are skipped
- Label column is optional (defaults to `House N`)
- Duplicate detection threshold: **~11 metres**

---

## 🔌 REST API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/houses` | List all houses |
| `POST` | `/api/houses` | Create house (triggers crop generation) |
| `GET` | `/api/houses/{id}` | Get house with crop info |
| `PUT` | `/api/houses/{id}` | Update coordinates (regenerates crops) |
| `DELETE` | `/api/houses/{id}` | Delete house + crop files |
| `POST` | `/api/import/check` | Pre-flight duplicate check (no DB write) |
| `POST` | `/api/import/confirmed` | Commit import after conflict resolution |
| `GET` | `/api/export/csv` | Download all houses as CSV |
| `GET` | `/api/houses/{id}/crops` | List crop records |
| `POST` | `/api/houses/{id}/crops/regenerate` | Re-crop from current coordinates |

---

## 🗄️ Database Schema

```sql
CREATE TABLE houses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT,
    lat1 REAL, lon1 REAL, lat2 REAL, lon2 REAL,
    lat3 REAL, lon3 REAL, lat4 REAL, lon4 REAL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE crops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    house_id INTEGER REFERENCES houses(id) ON DELETE CASCADE,
    year INTEGER,
    file_path TEXT,
    file_size INTEGER,
    width INTEGER,
    height INTEGER,
    zoom INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
```

---

## ⚙️ Configuration

Top of `main.py`:

```python
DB_PATH   = Path("satview.db")        # SQLite location
CROPS_DIR = Path("crops")             # Image output folder
CROP_ZOOM = 19                        # Tile zoom (higher = more detail)
YEARS     = [2025, 2024, 2023, 2022]  # Years to generate crops for
```

Frontend API URL (line 1 of `<script>` in `static/index.html`):

```javascript
const API = 'http://localhost:8000/api';
```

---

## 🔄 Duplicate Detection

Every CSV row is compared against all DB records. A **duplicate** is when all 4 corners are within `0.0001°` (~11m) of an existing record.

Per-conflict resolution options:
- **Keep DB** — skip the CSV row, existing record unchanged
- **Use CSV** — overwrite DB coords and regenerate crops
- **Keep Both** — insert CSV row as a new record alongside existing

The Confirm Import button stays locked until every conflict is resolved.

---

## 🛣️ Roadmap

- [ ] PostgreSQL support
- [ ] GeoJSON import/export
- [ ] Authentication / multi-user
- [ ] Map-click to draw new boundaries interactively
- [ ] Configurable year range
- [ ] Bulk coordinate correction

---

## 🤝 Contributing

1. Fork the repo
2. Create your branch: `git checkout -b feature/your-feature`
3. Commit: `git commit -m 'feat: add your feature'`
4. Push: `git push origin feature/your-feature`
5. Open a Pull Request

---

## 📄 License

[MIT](LICENSE)
