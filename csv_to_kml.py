"""
SatView CSV to KML converter
Usage:
    python csv_to_kml.py                          # uses default paths below
    python csv_to_kml.py my_export.csv out.kml    # custom paths
"""

import os
import re
import sys
import pandas as pd
import numpy as np
from xml.etree.ElementTree import Element, SubElement, tostring
import xml.dom.minidom as minidom

# ── Configuration ──────────────────────────────────────────────────────────────
DEFAULT_INPUT_CSV  = 'satview/MODELTOWN_satview_export_FINAL.csv'
DEFAULT_OUTPUT_KML = 'satview/MODELTOWN_satview_export_FINAL.kml'

INPUT_CSV  = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INPUT_CSV
OUTPUT_KML = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUTPUT_KML


# ── Safe value helpers ─────────────────────────────────────────────────────────

def _is_blank(v):
    if v is None:
        return True
    if isinstance(v, float) and np.isnan(v):
        return True
    return str(v).strip() == ''

def safe_str(v, default='--'):
    return default if _is_blank(v) else str(v).strip()

def safe_float_fmt(v, fmt='.1f', default='--'):
    if _is_blank(v):
        return default
    try:
        return format(float(v), fmt)
    except (TypeError, ValueError):
        return default


# ── WKT parser ─────────────────────────────────────────────────────────────────

def wkt_to_kml_coords(wkt):
    """Parse WKT POLYGON and return KML coordinates string 'lon,lat,0 ...' or None."""
    if _is_blank(wkt):
        return None
    wkt = str(wkt).strip().strip('"')
    m = re.search(r'POLYGON\s*\(\((.+?)\)\)', wkt, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    tokens = [t.strip() for t in m.group(1).split(',') if t.strip()]
    coords = []
    for tok in tokens:
        parts = tok.split()
        if len(parts) >= 2:
            coords.append(f"{parts[0]},{parts[1]},0")
    return ' '.join(coords) if coords else None


# ── Style helpers ──────────────────────────────────────────────────────────────
# KML color format: AABBGGRR (alpha, blue, green, red - all hex)

def _boundary_style_id(row):
    status = safe_str(row.get('constructed_2025', ''), '').lower()
    if status == 'non_constructed':
        return 'style_non_constructed'
    if status == 'constructed':
        hcls = safe_str(row.get('height_class_2025', ''), '').lower()
        if any(str(x) in hcls for x in ['4', '5', '6', '7', '8', '9']):
            return 'style_constructed_tall'
        if '3' in hcls:
            return 'style_constructed_high'
        if '2' in hcls:
            return 'style_constructed_medium'
        if '1' in hcls or hcls == '0':
            return 'style_constructed_low'
        return 'style_constructed_medium'
    return 'style_unclassified'

def _add_document_styles(doc_el):
    def _style(sid, poly_abgr, line_abgr, line_width, fill):
        s  = SubElement(doc_el, 'Style', id=sid)
        ls = SubElement(s, 'LineStyle')
        SubElement(ls, 'color').text  = line_abgr
        SubElement(ls, 'width').text  = str(line_width)
        ps = SubElement(s, 'PolyStyle')
        SubElement(ps, 'color').text   = poly_abgr
        SubElement(ps, 'fill').text    = '1' if fill else '0'
        SubElement(ps, 'outline').text = '1'

    # Boundary styles (semi-transparent fill, coloured outline)
    _style('style_constructed_tall',   '660000cc', 'ff0000cc', 2.5, True)  # deep red  G+4+
    _style('style_constructed_high',   '6600aaff', 'ff00aaff', 2.0, True)  # orange    G+3
    _style('style_constructed_medium', '6600cc44', 'ff00cc44', 2.0, True)  # green     G+2
    _style('style_constructed_low',    '66ffcc00', 'ffffcc00', 1.5, True)  # yellow    G+1
    _style('style_non_constructed',    '660000ff', 'ff0000ff', 2.0, True)  # red       non-constructed
    _style('style_unclassified',       '66888888', 'ff888888', 1.5, True)  # grey      no status

    # SAM mask styles (outline only, no fill - shows building footprint border)
    _style('style_mask_default',    '00000000', 'ffff8800', 1.5, False)  # orange outline
    _style('style_mask_non_constr', '00000000', 'ff4444ff', 1.5, False)  # blue outline


# ── Description HTML ───────────────────────────────────────────────────────────

def _build_description(row):
    bnd_note  = '<br><b>** Boundary manually edited **</b>' if safe_str(row.get('boundary_edited', '0'), '0') == '1' else ''
    mask_note = '<br><b>** Mask manually edited **</b>'     if safe_str(row.get('mask_edited',    '0'), '0') == '1' else ''
    return (
        f"<b>Plot:</b> {safe_str(row.get('label'))}<br>"
        f"<b>Point ID:</b> {safe_str(row.get('point_id'))}<br>"
        f"<b>Cluster:</b> {safe_str(row.get('cluster_id'))}  "
        f"<b>Plot Index:</b> {safe_str(row.get('plot_index'))}<br>"
        "<hr>"
        f"<b>Height 2023:</b> {safe_float_fmt(row.get('height_m_2023'))} m "
        f"({safe_str(row.get('height_class_2023'))})<br>"
        f"<b>Height 2024:</b> {safe_float_fmt(row.get('height_m_2024'))} m "
        f"({safe_str(row.get('height_class_2024'))})<br>"
        f"<b>Height 2025:</b> {safe_float_fmt(row.get('height_m_2025'))} m "
        f"({safe_str(row.get('height_class_2025'))})<br>"
        "<hr>"
        f"<b>Status 2025:</b> {safe_str(row.get('constructed_2025'))}<br>"
        f"<b>Status 2024:</b> {safe_str(row.get('constructed_2024'))}<br>"
        f"<b>Status 2023:</b> {safe_str(row.get('constructed_2023'))}<br>"
        "<hr>"
        f"<b>Plot Area (current):</b> {safe_float_fmt(row.get('sam_area_m2'))} m2<br>"
        f"<b>Plot Area (original):</b> {safe_float_fmt(row.get('plot_area_m2'))} m2<br>"
        f"<b>SAM Score:</b> {safe_float_fmt(row.get('sam_score'), '.3f')}<br>"
        f"<b>SAM IoU:</b> {safe_float_fmt(row.get('sam_iou'), '.3f')}<br>"
        f"<b>Covered:</b> {safe_float_fmt(row.get('covered_pct'))} %<br>"
        f"<b>SAM Status:</b> {safe_str(row.get('sam_status'))}<br>"
        f"<b>SAM Error:</b> {safe_str(row.get('sam_error'))}<br>"
        f"{bnd_note}{mask_note}"
    )


# ── KML polygon element builder ────────────────────────────────────────────────

def _add_polygon_element(parent_el, coords_kml):
    poly_el = SubElement(parent_el, 'Polygon')
    SubElement(poly_el, 'extrude').text      = '0'
    SubElement(poly_el, 'altitudeMode').text = 'clampToGround'
    outer = SubElement(poly_el, 'outerBoundaryIs')
    ring  = SubElement(outer, 'LinearRing')
    SubElement(ring, 'coordinates').text = coords_kml


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(INPUT_CSV):
        print(f"ERROR: File not found: {INPUT_CSV}")
        sys.exit(1)

    # Read all columns as strings - uniform NaN handling, no float conversion surprises
    df = pd.read_csv(INPUT_CSV, dtype=str)
    df = df.where(pd.notna(df), None)

    # Drop unnamed trailing columns (common from Excel/Sheets exports)
    df = df.loc[:, ~df.columns.str.startswith('Unnamed')]

    total = len(df)
    print(f"Loaded {total} rows from: {INPUT_CSV}")

    # ── Build KML document ─────────────────────────────────────────────────────
    kml = Element('kml', xmlns='http://www.opengis.net/kml/2.2')
    doc = SubElement(kml, 'Document')
    base_name = os.path.splitext(os.path.basename(INPUT_CSV))[0]
    SubElement(doc, 'name').text = base_name
    SubElement(doc, 'description').text = (
        'SatView export -- plot boundaries and SAM building footprint masks.\n'
        'Colour legend (based on 2025 status + height):\n'
        '  Red      = non-constructed\n'
        '  Green    = constructed G+2\n'
        '  Yellow   = constructed G+1\n'
        '  Orange   = constructed G+3\n'
        '  Deep red = constructed G+4+\n'
        '  Grey     = no status set'
    )

    _add_document_styles(doc)

    # Two separate folders for clean layer management in Google Earth / QGIS
    folder_bnd  = SubElement(doc, 'Folder')
    SubElement(folder_bnd,  'name').text = 'Plot Boundaries'

    folder_mask = SubElement(doc, 'Folder')
    SubElement(folder_mask, 'name').text = 'SAM Masks (building footprints)'

    skipped_bnd  = 0
    skipped_mask = 0

    for idx, row in df.iterrows():
        label = safe_str(row.get('label'), f"Row {idx + 1}")
        desc  = _build_description(row)

        # Boundary placemark
        bnd_coords = wkt_to_kml_coords(row.get('polygon_wkt'))
        if bnd_coords:
            pm = SubElement(folder_bnd, 'Placemark')
            SubElement(pm, 'name').text        = label
            SubElement(pm, 'description').text = desc
            SubElement(pm, 'styleUrl').text    = f"#{_boundary_style_id(row)}"
            _add_polygon_element(pm, bnd_coords)
        else:
            skipped_bnd += 1

        # SAM mask placemark
        mask_coords = wkt_to_kml_coords(row.get('sam_mask_wkt'))
        if mask_coords:
            status     = safe_str(row.get('constructed_2025'), '').lower()
            mask_style = 'style_mask_non_constr' if status == 'non_constructed' else 'style_mask_default'
            pm2 = SubElement(folder_mask, 'Placemark')
            SubElement(pm2, 'name').text        = f"{label} [mask]"
            SubElement(pm2, 'description').text = desc
            SubElement(pm2, 'styleUrl').text    = f"#{mask_style}"
            _add_polygon_element(pm2, mask_coords)
        else:
            skipped_mask += 1

    # ── Write output ───────────────────────────────────────────────────────────
    xml_str    = tostring(kml, encoding='unicode')
    pretty_xml = minidom.parseString(xml_str).toprettyxml(indent='  ')

    with open(OUTPUT_KML, 'w', encoding='utf-8') as f:
        f.write(pretty_xml)

    print(f"KML created: {OUTPUT_KML}")
    print(f"  Boundaries written : {total - skipped_bnd} / {total}")
    print(f"  SAM masks written  : {total - skipped_mask} / {total}")
    if skipped_bnd:
        print(f"  WARNING: {skipped_bnd} row(s) had missing/invalid polygon_wkt and were skipped")


if __name__ == '__main__':
    main()
