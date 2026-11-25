#!/usr/bin/env python3
"""
Extract 3D building footprints + heights from a city .osm.pbf file
and write a GeoJSON FeatureCollection to buildings-3d.json.
"""

import argparse
import json
import math
import re
import sys
import osmium


def parse_height(val):
    if not val:
        return None
    s = str(val).strip().lower()
    if not s:
        return None
    m = re.match(r'^([0-9]+(?:\.[0-9]+)?)\s*(m|meter|meters|metre|metres|ft|feet|\'|")?$', s)
    if not m:
        m2 = re.search(r'([0-9]+(?:\.[0-9]+)?)', s)
        if not m2:
            return None
        num = float(m2.group(1))
        return num if num > 0 else None
    num = float(m.group(1))
    unit = m.group(2) or 'm'
    if unit in ('ft', 'feet', "'"):
        num *= 0.3048
    elif unit == '"':
        num *= 0.0254
    return num if num > 0 else None


def parse_levels(val):
    if not val:
        return None
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)', str(val))
    if not m:
        return None
    num = float(m.group(1))
    return num if num > 0 else None


def procedural_height(cx, cy, idx):
    seed = abs(round(cx * 1e5 + cy * 1e5 + idx * 137))
    tier = seed % 16
    if tier == 0:
        h = 160.0 + (seed % 65) * 1.25
    elif tier <= 2:
        h = 105.0 + (seed % 40) * 1.15
    elif tier <= 5:
        h = 65.0 + (seed % 30) * 1.05
    elif tier <= 9:
        h = 38.0 + (seed % 20)
    else:
        h = 24.0 + (seed % 10)
    return round(h, 1)


class BuildingHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.features = []
        self.with_height = 0
        self.max_height = 0.0

    def area(self, a):
        tags = a.tags
        b_tag = tags.get('building')
        if not b_tag or b_tag == 'no':
            return

        h = parse_height(tags.get('height')) or parse_height(tags.get('building:height'))
        levels = parse_levels(tags.get('building:levels'))
        if h is None and levels is not None:
            h = round(levels * 3.2, 1)

        min_h = parse_height(tags.get('min_height')) or parse_height(tags.get('building:min_height'))
        if min_h is None:
            min_lvl = parse_levels(tags.get('building:min_level'))
            if min_lvl is not None:
                min_h = round(min_lvl * 3.2, 1)
            else:
                min_h = 0.0

        try:
            outer_rings = list(a.outer_rings())
        except Exception:
            return
        if not outer_rings:
            return

        for ring in outer_rings:
            coords = []
            sum_lon = 0.0
            sum_lat = 0.0
            for node in ring:
                if not node.location.valid():
                    continue
                lon = round(node.lon, 6)
                lat = round(node.lat, 6)
                coords.append([lon, lat])
                sum_lon += lon
                sum_lat += lat

            if len(coords) < 4:
                continue
            if coords[0] != coords[-1]:
                coords.append(coords[0])

            cx = sum_lon / len(coords)
            cy = sum_lat / len(coords)
            idx = len(self.features)

            if h is not None and h > 0:
                final_h = round(h, 1)
                self.with_height += 1
                if final_h > self.max_height:
                    self.max_height = final_h
            else:
                final_h = procedural_height(cx, cy, idx)

            final_levels = int(round(levels)) if levels else max(1, int(round(final_h / 3.2)))

            props = {
                'height': final_h,
                'min_height': round(min_h, 1) if min_h else 0,
                'levels': final_levels,
            }
            if b_tag != 'yes':
                props['kind'] = b_tag
            year = tags.get('start_date') or tags.get('year_built')
            if year:
                props['year_built'] = year
            hn = tags.get('addr:housenumber')
            if hn:
                props['addr_housenumber'] = hn
            st = tags.get('addr:street')
            if st:
                props['addr_street'] = st

            self.features.append({
                'type': 'Feature',
                'geometry': {
                    'type': 'Polygon',
                    'coordinates': [coords],
                },
                'properties': props,
            })
            break  # primary outer ring per building area


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pbf', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--city', default='city')
    args = parser.parse_args()

    handler = BuildingHandler()
    handler.apply_file(args.pbf, locations=True, idx='flex_mem')

    fc = {
        'type': 'FeatureCollection',
        'features': handler.features,
    }
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(fc, f, separators=(',', ':'))

    print(
        f"[{args.city}] Wrote {len(handler.features)} buildings "
        f"({handler.with_height} with OSM height/levels, max {handler.max_height}m) -> {args.out}"
    )


if __name__ == '__main__':
    main()
