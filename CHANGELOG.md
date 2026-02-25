# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.0] — 2025-02-25

### Added
- FastAPI backend with SQLite persistence
- CSV import with automatic duplicate detection (pre-flight check)
- Side-by-side conflict resolution UI (Keep DB / Use CSV / Keep Both)
- Apply-to-all bulk resolution for multiple conflicts
- 4-year historical satellite imagery per house via Esri Wayback (2022–2025)
- Background satellite image crop generation using Pillow
- Crop images stored as JPEG with DB path tracking
- Draggable boundary corner editing on live satellite tiles
- Real-time polygon sync across all 4 year maps when dragging
- Save-to-DB button for edited coordinates
- CSV export (from DB if online, browser fallback if offline)
- Load from CSV / Load from Database / Load Both (merge) modes
- Offline fallback mode when backend is not running
- Crop viewer modal with file path, dimensions, file size
- Delete house (removes DB record + crop files)
- DB status indicator bar
- Progress overlay during async operations
