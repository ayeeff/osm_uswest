#!/usr/bin/env python3
"""
Build the 2D dark city map datasets (places.json + demand-streets.json) for one
city from its OSM PBF extract.

Reads the PBF directly with pyosmium in a single pass. An earlier version drove
`osmium export` with a per-object-type config, which silently dropped every open
highway way unless `linear_tags` was declared as a list of tag filters — and
osmium export is tag-driven, so anything matching neither list is simply not
written. Parsing the PBF ourselves removes that whole class of failure and lets
`with_locations()` resolve the node references ways need.

  places.json         FeatureCollection<Point>  { name, rank, kind }
  demand-streets.json FeatureCollection<Line>   { id, demand }

Ported from workers/atlas-2d/src/assemble.js in ayeeff/astrogl so the Overpass
path and this path produce the same shape. Change a constant in one, change it
in both.
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import osmium
from osmium.osm import Node, Relation, Way

# --------------------------------------------------------------------------
# Tag groups. Mirrors POI_GROUPS in assemble.js / queries.js.
# `rank` drives label importance, `cap` bounds the per-kind feature count.
# --------------------------------------------------------------------------

POI_GROUPS = [
    {"id": "air", "kind": "air", "rank": 70, "cap": 5},
    {"id": "landmark", "kind": "landmark", "rank": 5, "cap": 600},
    {"id": "employment", "kind": "employment", "rank": 5, "cap": 600},
    {"id": "shop", "kind": "shop", "rank": 7, "cap": 500},
    {"id": "edu", "kind": "edu", "rank": 12, "cap": 250},
    {"id": "health", "kind": "health", "rank": 6, "cap": 250},
    {"id": "night", "kind": "night", "rank": 2, "cap": 250},
]
GROUP_BY_KIND = {g["kind"]: g for g in POI_GROUPS}

DISTRICT_RANK = {6: 4, 7: 4, 8: 4, 9: 5, 10: 6, 11: 6, 12: 6}

# Named place nodes/areas make a better district source than admin boundaries in
# much of the world — China's OSM coverage of admin_level 8-10 is thin, while
# place tagging is dense. village/hamlet are excluded: they fall below the
# style's rank>=4 label threshold, so they would draw a district dot with no
# label.
PLACE_KINDS = {
    "city": 6,
    "town": 5,
    "borough": 5,
    "suburb": 5,
    "quarter": 4,
    "neighbourhood": 4,
    "district": 5,
}

# Deliberately the same three classes atlas-2d-worker uses. footway, path and
# cycleway are excluded on both sides: they dominate the way count in any city
# and carry no demand signal.
STREET_CLASSES = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "residential", "unclassified", "living_street",
    "service", "pedestrian", "track",
}

NAME_KEYS = ("name", "name:en", "name:zh", "name:zh-Hans", "name:ja", "name:ko")

# --------------------------------------------------------------------------
# Demand scoring constants — identical to assemble.js
# --------------------------------------------------------------------------

ROAD_BOOST = {
    "motorway": 1.8,
    "trunk": 1.7,
    "primary": 1.6,
    "secondary": 1.35,
    "tertiary": 1.2,
    "residential": 1.0,
    "unclassified": 0.9,
    "living_street": 0.85,
    "pedestrian": 0.7,
    "track": 0.5,
    "service": 0.4,
}

KIND_WEIGHT = {
    "shop": 1,
    "edu": 1.5,
    "health": 1.5,
    "night": 2,
    "landmark": 2,
    "employment": 2.5,
    "air": 3,
    "district": 0.4,
}

RADIUS_DEG = 0.0015  # ~150 m of latitude
CELL = RADIUS_DEG * 2
PCT = 0.985
# Districts are uncapped in the output, so cap what goes into the scoring grid — enough to
# cover any realistic city centre, and it keeps the grid build bounded.
DISTRICT_GRID_CAP = 4000

# Neighborhood outlines. Below NEIGHBORHOOD_RANK_MIN the polygon is smaller than
# the map's click tolerance; below MIN_NEIGHBORHOOD_KM2 it is not worth drawing;
# above MAX_NEIGHBORHOOD_KM2 it is a region or a coastline, not a neighborhood
# you would click on a city map (a place=area coastline ring came through at
# 1400 km2 before this cap).
NEIGHBORHOOD_RANK_MIN = 4
MIN_NEIGHBORHOOD_KM2 = 0.4
MAX_NEIGHBORHOOD_KM2 = 400.0
# A / P^2 ceiling from the isoperimetric inequality (1 / 4pi = 0.0796), with a
# little slack for the km approximation. Real boundaries measure 0.013-0.03.
ISOPERIMETRIC_MAX = 0.075


def round5(n):
    return round(n, 5)


def name_of(tags):
    for key in NAME_KEYS:
        value = tags.get(key)
        if value:
            return value
    return None


def classify(tags):
    """Map OSM tags to one of our kinds, or None."""
    if tags.get("boundary") == "administrative":
        level = tags.get("admin_level")
        if level and str(level).isdigit() and int(level) in DISTRICT_RANK:
            return "district"
    place = tags.get("place")
    if place in PLACE_KINDS and name_of(tags):
        return "place"
    if tags.get("aeroway") == "aerodrome":
        return "air"
    if tags.get("station") == "airport" and tags.get("railway") == "station":
        return "air"

    if tags.get("tourism") in {
        "museum", "attraction", "artwork", "viewpoint", "gallery",
        "theme_park", "zoo", "aquarium",
    }:
        return "landmark"
    if tags.get("historic"):
        return "landmark"
    if tags.get("man_made") in {"tower", "lighthouse", "bridge", "obelisk", "statue"}:
        return "landmark"
    if tags.get("leisure") in {"stadium", "arena", "park"} and name_of(tags):
        return "landmark"

    if tags.get("amenity") in {"bank", "stock_exchange", "courthouse", "townhall"}:
        return "employment"
    if tags.get("building") == "office" and name_of(tags):
        return "employment"
    if tags.get("office"):
        return "employment"
    if tags.get("shop"):
        return "shop"
    if tags.get("amenity") in {"school", "college", "university", "kindergarten", "library"}:
        return "edu"
    if tags.get("healthcare"):
        return "health"
    if tags.get("amenity") in {"hospital", "clinic", "doctors", "pharmacy"}:
        return "health"
    if tags.get("amenity") in {
        "restaurant", "bar", "pub", "cafe", "fast_food", "nightclub", "wine_bar",
    }:
        return "night"
    if tags.get("leisure") in {"nightclub", "bar", "pub"}:
        return "night"
    return None


def parse_bbox(value):
    """'minLng,minLat,maxLng,maxLat' -> (min_lng, min_lat, max_lng, max_lat)."""
    if not value:
        return None
    parts = [float(p) for p in value.split(",")]
    if len(parts) != 4:
        raise ValueError("--bbox must be minLng,minLat,maxLng,maxLat")
    return tuple(parts)


def ring_hits_bbox(ring, bbox):
    if bbox is None:
        return True
    min_lng, min_lat, max_lng, max_lat = bbox
    for lon, lat in ring:
        if min_lng <= lon <= max_lng and min_lat <= lat <= max_lat:
            return True
    return False


def _int_or_none(value):
    """Parse an OSM population-ish tag, returning None rather than junk."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text:
        return None
    # Tolerate "2023-01-01" style census dates and "1.234" thousands marks.
    mult = 1
    if text.endswith("k"):
        mult, text = 1000, text[:-1]
    elif text.endswith("m"):
        mult, text = 1000000, text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        return None


def shoelace2(ring):
    total = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[i + 1][0], ring[i + 1][1]
        total += x0 * y1 - x1 * y0
    return total


def ring_bbox(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def ring_contained_frac(ring, bbox):
    """
    Fraction of the ring's OWN bounding box that falls inside the city bbox.

    A district cannot be much bigger than the city we enrolled, so anything that
    mostly lies outside is not that city's district. This is what keeps a hamlet
    relation in Lower Saxony out of Berlin's map.
    """
    minx, miny, maxx, maxy = ring_bbox(ring)
    w = max(maxx - minx, 1e-9)
    h = max(maxy - miny, 1e-9)
    ox = max(0.0, min(maxx, bbox[2]) - max(minx, bbox[0]))
    oy = max(0.0, min(maxy, bbox[3]) - max(miny, bbox[1]))
    return (ox * oy) / (w * h)


def stitch_rings(segments):
    """
    Walk the member ways into CLOSED rings and return every ring found.

    Needed because the overwhelming majority of real administrative boundaries
    are legacy `type=boundary` relations, not `type=multipolygon`. libosmium's
    area assembler only builds areas for multipolygons, so with_areas() hands
    back an EMPTY geometry for them — which is why an earlier version of this
    script returned 12 neighbourhoods for Berlin instead of ~190.

    Only rings that close on themselves are returned. An earlier version kept
    whatever partial chain the greedy merge happened to reach, which is how a
    5-point, 384 km2 "neighborhood" named "Elbe-Luebeck-Kanal" ended up on
    Berlin's map: the member ways never formed a loop at all.
    """
    adjacency = defaultdict(list)
    closed = []
    segs = []
    for seg in segments:
        if len(seg) < 2:
            continue
        if seg[0] == seg[-1] and len(seg) > 3:
            closed.append(list(seg))   # a single way that is already a loop
            continue
        i = len(segs)
        segs.append(list(seg))
        adjacency[(seg[0][0], seg[0][1])].append(i)
        adjacency[(seg[-1][0], seg[-1][1])].append(i)

    used = [False] * len(segs)
    for seed in range(len(segs)):
        if used[seed]:
            continue
        used[seed] = True
        ring = list(segs[seed])
        while True:
            cur = (ring[-1][0], ring[-1][1])
            start = (ring[0][0], ring[0][1])
            if cur == start:
                break
            nxt = None
            for i in adjacency.get(cur, ()):
                if not used[i]:
                    nxt = i
                    break
            if nxt is None:
                break
            used[nxt] = True
            seg = segs[nxt]
            if (seg[0][0], seg[0][1]) == cur:
                ring.extend(seg[1:])
            else:
                ring.extend(reversed(seg[:-1]))
        if len(ring) >= 4 and (ring[0][0], ring[0][1]) == (ring[-1][0], ring[-1][1]):
            closed.append(ring)
    return closed


def is_boundary_ish(tags):
    if not tags:
        return False
    if tags.get("boundary") == "administrative":
        level = tags.get("admin_level")
        if level and str(level).isdigit() and int(level) in DISTRICT_RANK:
            return True
    return tags.get("place") in PLACE_KINDS


def collect_boundaries(pbf, bbox):
    """
    Assemble boundary/place polygons by hand, in two passes over the PBF.

    Pass 1 finds the relations and the way ids they reference. Pass 2 keeps
    coordinates only for those ways. OSM PBF orders nodes -> ways -> relations,
    so a single pass cannot know the wanted way ids while it is still reading the
    ways; and a city has far too many ways to hold them all.
    """
    wanted = {}   # way id -> True
    relations = []  # (tags, [way refs])

    for obj in osmium.FileProcessor(pbf):
        if not isinstance(obj, Relation):
            continue
        # pyosmium reuses the object buffer, so `obj.tags` is only valid inside
        # this callback — copy it out before it is used later.
        tags = {t.k: t.v for t in obj.tags}
        if not is_boundary_ish(tags):
            continue
        refs = [m.ref for m in obj.members if m.type == "w"]
        if not refs:
            continue
        relations.append((tags, refs))
        for ref in refs:
            wanted[ref] = True

    if not relations:
        return []

    way_coords = {}
    for obj in osmium.FileProcessor(pbf).with_locations():
        if not isinstance(obj, Way) or obj.id not in wanted:
            continue
        coords = []
        for nd in obj.nodes:
            loc = nd.location
            if loc.valid():
                coords.append((loc.lon, loc.lat))
        if len(coords) >= 2:
            way_coords[obj.id] = coords

    out = []
    for tags, refs in relations:
        segments = [way_coords[r] for r in refs if r in way_coords]
        if not segments:
            continue
        for ring in stitch_rings(segments):
            if not ring_hits_bbox(ring, bbox):
                continue
            if ring_contained_frac(ring, bbox) < 0.5:
                continue
            ring = [(round5(p[0]), round5(p[1])) for p in ring]
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            out.append((tags, ring))
    return out


class Collector(osmium.SimpleHandler):
    def __init__(self, bbox=None):
        super().__init__()
        self.bbox = bbox
        self.areas = defaultdict(list)  # kind -> [(name, lon, lat, rank)]
        self.streets = []               # (score, highway, osm_id, [[lon, lat], ...])
        self.neighborhoods = []         # dicts with polygon geometry
        self.counts = defaultdict(int)
        self._grid = None
        self._nearby = None
        self.ways_seen = 0

    def _add_neighborhood(self, tags, ring, source):
        """Keep a named administrative/place polygon as a clickable neighborhood."""
        if not tags or not ring or len(ring) < 4:
            return
        name = name_of(tags)
        if not name:
            return
        place = tags.get("place")
        level = tags.get("admin_level")
        if level and str(level).isdigit() and int(level) in DISTRICT_RANK:
            rank = DISTRICT_RANK[int(level)]
        elif place in PLACE_KINDS:
            rank = PLACE_KINDS[place]
        else:
            return
        if not (NEIGHBORHOOD_RANK_MIN <= rank <= 9):
            return
        self.neighborhoods.append(
            {
                "name": name,
                "rank": rank,
                "place": place or ("boundary" if source == "relation" else "area"),
                "wikidata": tags.get("wikidata") or tags.get("wikidata_ref") or "",
                # Sparse, but free and authoritative where it exists, so prefer
                # it over anything Wikidata has for the same place.
                "osm_population": _int_or_none(tags.get("population")),
                "ring": ring,
            }
        )

    # The POI grid is built lazily on the first way. OSM PBF is node-sorted, so
    # every node — and therefore every POI — has already been seen by the time
    # the first way arrives, which is what makes streaming scoring possible.
    def _ensure_grid(self):
        if self._grid is not None:
            return
        grid = defaultdict(list)
        for kind, items in self.areas.items():
            weight = KIND_WEIGHT.get(kind, 1)
            # Districts are uncapped in the output, so cap the grid snapshot to
            # keep it bounded; the per-kind caps match the final output.
            cap = DISTRICT_GRID_CAP if kind == "district" else GROUP_BY_KIND[kind]["cap"]
            for _name, lon, lat, _rank in items[:cap]:
                grid[(math.floor(lon / CELL), math.floor(lat / CELL))].append((lon, lat, weight))
        self._grid = grid

        r2 = RADIUS_DEG * RADIUS_DEG
        cache = {}

        def nearby(lon, lat):
            # Way vertices repeat heavily along a street; memoise on a coarse key.
            key = (round(lon, 4), round(lat, 4))
            hit = cache.get(key)
            if hit is not None:
                return hit
            total = 0
            gx = math.floor(lon / CELL)
            gy = math.floor(lat / CELL)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for px, py, weight in grid.get((gx + dx, gy + dy), ()):
                        ddx = px - lon
                        ddy = py - lat
                        if ddx * ddx + ddy * ddy <= r2:
                            total += weight
            cache[key] = total
            return total

        self._nearby = nearby

    def _add_area(self, tags, lon, lat):
        if self.bbox:
            min_lng, min_lat, max_lng, max_lat = self.bbox
            if not (min_lng <= lon <= max_lng and min_lat <= lat <= max_lat):
                return
        kind = classify(tags)
        if kind is None:
            return
        name = name_of(tags)
        if not name:
            return
        if kind == "district":
            level = tags.get("admin_level")
            rank = DISTRICT_RANK.get(int(level), 5) if level and str(level).isdigit() else 5
            self.areas["district"].append((name, lon, lat, rank))
        elif kind == "place":
            self.areas["district"].append((name, lon, lat, PLACE_KINDS[tags["place"]]))
        else:
            group = GROUP_BY_KIND[kind]
            self.areas[kind].append((name, lon, lat, group["rank"]))
        self.counts[kind] += 1

    def node(self, n):
        if not n.tags:
            return
        loc = n.location
        if not loc.valid():
            return
        self._add_area(n.tags, round5(loc.lon), round5(loc.lat))

    def way(self, w):
        highway = w.tags.get("highway") if w.tags else None
        wants_street = highway in STREET_CLASSES
        wants_area = bool(w.tags) and (
            w.tags.get("boundary") == "administrative" or w.tags.get("place") in PLACE_KINDS
        )
        if not wants_street and not wants_area:
            return
        if wants_street:
            self.ways_seen += 1

        coords = []
        for nd in w.nodes:
            loc = nd.location
            if loc.valid():
                coords.append((round5(loc.lon), round5(loc.lat)))

        if wants_street and len(coords) >= 2:
            if self.bbox:
                min_lng, min_lat, max_lng, max_lat = self.bbox
                if not any(
                    min_lng <= lon <= max_lng and min_lat <= lat <= max_lat
                    for lon, lat in coords
                ):
                    coords = []
            if len(coords) < 2:
                return
            # Score now and keep only the streets that actually have POI signal.
            # Retaining every way in a big city exhausts runner memory; this is
            # the same reason atlas-2d-worker scores per tile instead of in one
            # assemble pass.
            self._ensure_grid()
            weight_sum = 0.0
            for lon, lat in coords:
                weight_sum += self._nearby(lon, lat)
            if weight_sum > 0:
                boost = ROAD_BOOST.get(highway, 1.0)
                score = (weight_sum / len(coords)) * boost
                self.streets.append((score, highway, w.id, coords))
        if wants_area and coords:
            self._add_area(w.tags, *centroid(coords))
            if w.tags.get("boundary") == "administrative" or w.tags.get("place") in PLACE_KINDS:
                self._add_neighborhood(w.tags, coords, "way")

    def relation(self, r):
        # Only reached with with_areas() enabled, so an administrative boundary
        # arrives with its multipolygon already assembled. RelationMember has no
        # .location in pyosmium, so member coordinates are not an option here.
        if not r.tags:
            return
        is_admin = r.tags.get("boundary") == "administrative"
        is_place = r.tags.get("place") in PLACE_KINDS
        if not is_admin and not is_place:
            return
        geometry = getattr(r, "geometry", None)
        if geometry is None:
            return
        ring = exterior_ring(geometry)
        if not ring or not ring_hits_bbox(ring, self.bbox):
            return
        lon, lat = centroid(ring)
        self._add_area(r.tags, round5(lon), round5(lat))
        self._add_neighborhood(r.tags, ring, "relation")


def exterior_ring(geometry):
    """First exterior ring of a Polygon/Multipolygon geometry, as [(lon, lat)]."""
    kind = geometry.type
    if kind == "Polygon":
        rings = list(geometry)
    elif kind == "MultiPolygon":
        rings = list(geometry)
        if not rings:
            return []
        rings = list(rings[0])
    else:
        return []
    if not rings:
        return []
    return [(p.lon, p.lat) for p in rings[0] if p.valid()]


def centroid(coords):
    """Shoelace centroid of a ring, falling back to the vertex mean."""
    if len(coords) < 3:
        n = len(coords) or 1
        return (
            sum(c[0] for c in coords) / n,
            sum(c[1] for c in coords) / n,
        )
    area2 = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(coords) - 1):
        x0, y0 = coords[i]
        x1, y1 = coords[i + 1]
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if area2 != 0:
        return cx / (3.0 * area2), cy / (3.0 * area2)
    n = len(coords)
    return sum(c[0] for c in coords) / n, sum(c[1] for c in coords) / n


def build_places(collector):
    features = []
    counts = {}
    seen = set()

    for name, lon, lat, rank in collector.areas.get("district", []):
        key = ("district", name, round5(lat), round5(lon))
        if key in seen:
            continue
        seen.add(key)
        features.append(
            {
                "type": "Feature",
                "properties": {"name": name, "rank": rank, "kind": "district"},
                "geometry": {"type": "Point", "coordinates": [round5(lon), round5(lat)]},
            }
        )
    counts["district"] = len(collector.areas.get("district", []))

    for group in POI_GROUPS:
        kind = group["kind"]
        picked = collector.areas.get(kind, [])
        # Prefer specific names when the cap bites, same as normalizePois().
        picked.sort(key=lambda item: (len(item[0]), item[0]))
        kept = 0
        for name, lon, lat, rank in picked[: group["cap"]]:
            key = (kind, name, round5(lat), round5(lon))
            if key in seen:
                continue
            seen.add(key)
            features.append(
                {
                    "type": "Feature",
                    "properties": {"name": name, "rank": rank, "kind": kind},
                    "geometry": {"type": "Point", "coordinates": [round5(lon), round5(lat)]},
                }
            )
            kept += 1
        counts[kind] = kept

    return features, counts


def build_demand(streets, max_streets):
    """Normalise the already-scored streets and emit the top `max_streets`."""
    if not streets:
        return []

    positives = sorted(s[0] for s in streets)
    cutoff = positives[min(len(positives) - 1, int(len(positives) * PCT))]

    streets.sort(key=lambda item: -item[0])
    out = []
    for score, _highway, osm_id, coords in streets[:max_streets]:
        demand = min(1.0, score / cutoff) if cutoff > 0 else 0.0
        out.append(
            {
                "type": "Feature",
                "properties": {"id": osm_id, "demand": round5(demand)},
                "geometry": {"type": "LineString", "coordinates": [list(p) for p in coords]},
            }
        )
    return out


def ring_area_km2(ring):
    """Planar shoelace area of a lon/lat ring, corrected for latitude."""
    if len(ring) < 4:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    kx = 111.320 * math.cos(math.radians(lat0))
    ky = 110.574
    area = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i][0] * kx, ring[i][1] * ky
        x1, y1 = ring[i + 1][0] * kx, ring[i + 1][1] * ky
        area += x0 * y1 - x1 * y0
    return abs(area) / 2.0


def ring_perimeter_km(ring):
    total = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        dy = (y1 - y0) * 110.574
        dx = (x1 - x0) * 111.320 * max(0.05, math.cos(math.radians((y0 + y1) / 2.0)))
        total += math.hypot(dx, dy)
    return total


def bbox_area_km2(bbox):
    """Rough planar area of the enrolled bbox, in km2."""
    minx, miny, maxx, maxy = bbox
    lat0 = (miny + maxy) / 2.0
    km_per_deg_lat = 110.574
    km_per_deg_lon = 111.320 * max(0.05, math.cos(math.radians(lat0)))
    return max((maxx - minx) * km_per_deg_lon, 0.0) * max((maxy - miny) * km_per_deg_lat, 0.0)


def build_neighborhoods(collector, max_neighborhoods):
    """
    Turn the collected rings into a FeatureCollection<Polygon>.

    Only the largest ring of each feature is kept — the map draws an outline, and
    a multipolygon with a hundred offshore islets would triple the payload for no
    visual gain. Rank >= NEIGHBORHOOD_RANK_MIN keeps village-scale places out
    (they would be dots inside their parent's polygon anyway).
    """
    best = {}
    bbox = collector.bbox
    # The area cap has to scale with the city. A flat 400 km2 cap silently
    # deleted most of Beijing's districts — Fangshan is 2301, Daxing 1462,
    # Changping 1343, Shunyi 1010 — so 海淀区/通州区/大兴区/昌平区 came back
    # missing and 朝阳区 fell back to a 10 km2 fragment. The containment check
    # below is what actually rejects region-sized rings, so the cap only has to
    # catch a polygon that is mostly the whole enrolled bbox.
    max_km2 = max(MAX_NEIGHBORHOOD_KM2, 0.9 * bbox_area_km2(bbox))
    for nb in collector.neighborhoods:
        name = nb["name"]
        area = ring_area_km2(nb["ring"])
        if area < MIN_NEIGHBORHOOD_KM2 or area > max_km2:
            continue
        if ring_contained_frac(nb["ring"], bbox) < 0.5:
            continue
        # The isoperimetric inequality: no planar shape encloses more area than
        # P^2 / 4pi, i.e. A / P^2 can never exceed 0.0796. Real district
        # boundaries land at 0.013-0.03. China's shared-border relations (the
        # 界 family) are lines, not areas, but their ways do join end to end, so
        # the stitcher closes them into a degenerate loop that reported things
        # like 7661 km2 inside a 3 km perimeter — A/P^2 of 860, four orders of
        # magnitude over the bound. This rejects them with huge margin and
        # cannot reject a genuine polygon.
        perim = ring_perimeter_km(nb["ring"])
        if perim <= 0.0 or (area / (perim * perim)) > ISOPERIMETRIC_MAX:
            continue
        # One polygon per name, keeping the LARGEST. OSM carries the same place
        # as several relations (admin_level 9 borough vs 10 sub-unit vs a
        # duplicate closed way), and first-wins let a 5.6 km2 "Pankow" shadow
        # the real 102 km2 borough.
        prev = best.get(name)
        if prev is None or area > prev[0]:
            best[name] = (area, nb)

    # Two differently-named relations can share one boundary (a joint
    # forest/district edge in the Beijing area gave 朝阳区 and 顺义区 the exact
    # same 174-point ring). Key on the geometry so only one survives.
    seen_geom = set()
    features = []
    for name, (area, nb) in sorted(best.items(), key=lambda kv: -kv[1][0]):
        gkey = (nb["place"], len(nb["ring"]), nb["ring"][0], nb["ring"][-1])
        if gkey in seen_geom:
            continue
        seen_geom.add(gkey)
        cx, cy = centroid(nb["ring"])
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "name": name,
                    "rank": nb["rank"],
                    "kind": nb["place"],
                    "areaKm2": round(area, 2),
                    "lon": round5(cx),
                    "lat": round5(cy),
                    "wikidata": nb["wikidata"],
                    "image": "",
                    # Filled by enrich_neighborhoods.py from Wikidata P1082/P18,
                    # except where OSM already carries a population tag.
                    "residents": nb.get("osm_population"),
                    "populationYear": None,
                    "imageSource": "",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[round5(p[0]), round5(p[1])] for p in nb["ring"]]],
                },
            }
        )
        if len(features) >= max_neighborhoods:
            break
    return {"type": "FeatureCollection", "features": features}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--pbf", required=True, help="OSM PBF: the country extract, or a city extract")
    parser.add_argument(
        "--bbox",
        default="",
        help="minLng,minLat,maxLng,maxLat — keep only objects intersecting it. Pass the "
        "city bbox when reading a whole country PBF.",
    )
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--extract", default="", help="source extract slug, for the manifest")
    parser.add_argument("--max-streets", type=int, default=4000)
    parser.add_argument("--max-neighborhoods", type=int, default=1500)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    collector = Collector(bbox=parse_bbox(args.bbox))
    # with_areas() assembles boundary multipolygons so relations carry geometry;
    # with_locations() resolves the node references ways are built from.
    #
    # Read the PBF WHOLE. Do not pre-cut it with `osmium extract`: its default
    # `simple` strategy drops the member ways of relations, so with_areas() cannot
    # assemble the multipolygon and every neighborhood silently vanishes. That is
    # exactly how Berlin came back with 13 outlines instead of ~190.
    for obj in osmium.FileProcessor(args.pbf).with_areas().with_locations():
        if isinstance(obj, Node):
            collector.node(obj)
        elif isinstance(obj, Way):
            collector.way(obj)

    # Boundaries come from a separate hand-assembled pass: with_areas() cannot
    # help with the legacy type=boundary relations that most real districts use.
    for tags, ring in collect_boundaries(args.pbf, collector.bbox):
        lon, lat = centroid(ring)
        collector._add_area(tags, round5(lon), round5(lat))
        collector._add_neighborhood(tags, ring, "relation")

    places, counts = build_places(collector)
    demand = build_demand(collector.streets, args.max_streets)
    neighborhoods = build_neighborhoods(collector, args.max_neighborhoods)

    places_doc = {"type": "FeatureCollection", "features": places}
    demand_doc = {"type": "FeatureCollection", "features": demand}

    places_path = os.path.join(args.out_dir, "places.json")
    demand_path = os.path.join(args.out_dir, "demand-streets.json")
    hoods_path = os.path.join(args.out_dir, "neighborhoods.json")
    with open(places_path, "w", encoding="utf-8") as handle:
        json.dump(places_doc, handle, separators=(",", ":"))
    with open(demand_path, "w", encoding="utf-8") as handle:
        json.dump(demand_doc, handle, separators=(",", ":"))
    with open(hoods_path, "w", encoding="utf-8") as handle:
        json.dump(neighborhoods, handle, separators=(",", ":"))

    max_demand = max((f["properties"]["demand"] for f in demand), default=0)
    manifest = {
        "slug": args.slug,
        "extract": args.extract,
        # atlas-2d-worker compares this against the R2 upload time of the copy it
        # published, to decide whether the pipeline output is newer than what the
        # site is serving. Without it the comparison is a no-op.
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "places": len(places),
        "byKind": counts,
        "streetWaysScanned": collector.ways_seen,
        "streetsWithSignal": len(collector.streets),
        "streets": len(demand),
        "neighborhoods": len(neighborhoods["features"]),
        "maxDemand": max_demand,
        "bytes": {
            "places.json": os.path.getsize(places_path),
            "demand-streets.json": os.path.getsize(demand_path),
            "neighborhoods.json": os.path.getsize(hoods_path),
        },
    }
    with open(os.path.join(args.out_dir, "_manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)

    print(json.dumps(manifest))
    if not places:
        print(f"warning: no places extracted for {args.slug}", file=sys.stderr)
        return 1
    if not demand:
        print(f"warning: no demand streets extracted for {args.slug}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
