#!/usr/bin/env python3
"""Extract transit LINE geometry (metro/train/tram/ferry routes) for one city.

The station counterpart is extract_transit_hubs.py, which emits points. Pages
also need the lines themselves: a metro atlas without its lines is a scatter of
dots.

Geometry comes from OSM route relations (type=route, route=subway|train|...),
assembled from their member ways. Relations are preferred over the raw railway
ways because a route carries the things a reader needs to draw it: a name, a line
ref, a colour, and an operator. Raw railway=* ways have none of those and
cannot be coloured per line.

When a route has no usable name or ref, it is emitted as an unnamed feature
rather than dropped, because a visible unnamed track beats a silent gap.

Usage (CI passes a pre-cut --pbf, same as the 2d pipeline):
  python extract_transit_lines.py --city hongkong --extract hongkong \
      --pbf work/city.osm.pbf --bbox 22.15,113.82,22.58,114.44 \
      --out transit-lines.json
"""
import argparse
import json
import sys
from collections import Counter

import osmium

RAIL_ROUTES = {"subway", "light_rail", "train", "tram", "monorail", "ferry",
               "light_rail", "subway"}
FERRY_ROUTES = {"ferry"}
RAIL_WAYS = {"subway", "light_rail", "monorail", "train", "tram", "rail"}


def norm(s):
    return (s or "").strip()


class Collector(osmium.SimpleHandler):
    """Two passes: relations first, then geometry for the ways they reference."""

    def __init__(self, min_lat, min_lon, max_lat, max_lon, simplify_m):
        super().__init__()
        self.bbox = (min_lat, min_lon, max_lat, max_lon)
        self.simplify_m = simplify_m
        self.routes = {}          # rel_id -> tags
        self.rel_ways = {}        # rel_id -> [way_id]
        self.way_nodes = {}       # way_id -> [node_id]
        self.node_xy = {}         # node_id -> (lon, lat)
        self.counts = Counter()

    def _in_box(self, lat, lon):
        return self.bbox[0] <= lat <= self.bbox[2] and self.bbox[1] <= lon <= self.bbox[3]

    def relation(self, r):
        t = dict(r.tags)
        if t.get("type") != "route":
            return
        route = t.get("route", "")
        if route not in RAIL_ROUTES:
            return
        members = [m.ref for m in r.members
                   if m.type == "w" and r.tags.get("route") is not None]
        if not members:
            return
        self.routes[r.id] = {
            "name": norm(t.get("name")),
            "ref": norm(t.get("ref")),
            "route": route,
            "colour": norm(t.get("colour") or t.get("color")),
            "operator": norm(t.get("operator")),
            "network": norm(t.get("network")),
        }
        self.rel_ways[r.id] = members
        self.counts[f"route:{route}"] += 1

    def way(self, w):
        if w.id in self.rel_ways.values():
            return
        # Defer: only record ways once we know a relation wants them.
        return

    def node(self, n):
        if n.location.valid():
            self.node_xy[n.id] = (n.location.lon, n.location.lat)


class GeometryCollector(osmium.SimpleHandler):
    """Second pass: capture coordinates only for ways some route referenced."""

    def __init__(self, wanted):
        super().__init__()
        self.wanted = wanted
        self.ways = {}

    def way(self, w):
        if w.id in self.wanted:
            pts = []
            for nd in w.nodes:
                xy = self.xy.get(nd.ref)
                if xy:
                    pts.append([round(xy[0], 5), round(xy[1], 5)])
            if len(pts) >= 2:
                self.ways[w.id] = pts


def rdp(points, tol_m):
    """Ramer-Douglas-Peucker on lon/lat with a metre-ish tolerance."""
    if len(points) < 3 or tol_m <= 0:
        return points
    # Work in a local metric plane; the tolerance is in metres.
    lat0 = points[0][1]
    kx = 111320.0 * __import__("math").cos(__import__("math").radians(lat0))
    ky = 110540.0

    def proj(p):
        return (p[0] * kx, p[1] * ky)

    pts = [proj(p) for p in points]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi <= lo + 1:
            continue
        ax, ay = pts[lo]
        bx, by = pts[hi]
        dx, dy = bx - ax, by - ay
        den = dx * dx + dy * dy
        best, besti = -1.0, -1
        for i in range(lo + 1, hi):
            if den == 0:
                d = (pts[i][0] - ax) ** 2 + (pts[i][1] - ay) ** 2
            else:
                t = ((pts[i][0] - ax) * dx + (pts[i][1] - ay) * dy) / den
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                px, py = ax + t * dx, ay + t * dy
                d = (pts[i][0] - px) ** 2 + (pts[i][1] - py) ** 2
            if d > best:
                best, besti = d, i
        if best > tol_m * tol_m:
            keep[besti] = True
            stack.append((lo, besti))
            stack.append((besti, hi))
    return [p for p, k in zip(points, keep) if k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--extract")
    ap.add_argument("--pbf", help="Pre-cut city PBF (CI passes this)")
    ap.add_argument("--bbox", required=True, help="minLat,minLon,maxLat,maxLon")
    ap.add_argument("--simplify", type=float, default=2.0,
                    help="RDP tolerance in metres for line vertices")
    ap.add_argument("--min-points", type=int, default=2)
    ap.add_argument("--network", action="append", default=[],
                    help="keep only these network values (repeatable)")
    ap.add_argument("--operator", action="append", default=[],
                    help="keep only these operator values (repeatable)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    min_lat, min_lon, max_lat, max_lon = (float(x) for x in args.bbox.split(","))
    if not args.pbf:
        raise SystemExit("--pbf required (cut the city first; see extract_transit_hubs.py)")

    c = Collector(min_lat, min_lon, max_lat, max_lon, args.simplify)
    c.apply_file(args.pbf)

    # A bbox does NOT bound relation geometry: OSM relations carry ways that run
    # to the relation's true extent, so a Shenzhen route in the "hongkong"
    # extract still comes through even when its coordinates sit outside the box.
    # Per-city network/operator rules are what actually bound a city's lines,
    # the same way they bound its stations.
    def keep(meta):
        if args.network and not any(m.casefold() in meta["network"].casefold()
                                     for m in args.network):
            return False
        if args.operator and not any(m.casefold() in meta["operator"].casefold()
                                     for m in args.operator):
            return False
        return True

    before = len(c.routes)
    c.routes = {k: v for k, v in c.routes.items() if keep(v)}
    filtered = before - len(c.routes)
    c.counts = Counter(v["route"] for v in c.routes.values())

    wanted = set()
    for rel_id, members in c.rel_ways.items():
        if rel_id in c.routes:
            wanted.update(members)

    g = GeometryCollector(wanted)
    g.xy = c.node_xy
    g.apply_file(args.pbf)

    features = []
    dropped_no_geom = 0
    unnamed = 0
    total_pts = 0
    for rel_id, meta in c.routes.items():
        coords = []
        for wid in c.rel_ways[rel_id]:
            pts = g.ways.get(wid)
            if pts:
                coords.extend(pts if not coords else pts[1:])
        coords = rdp(coords, args.simplify)
        if len(coords) < args.min_points:
            dropped_no_geom += 1
            continue
        if not meta["name"] and not meta["ref"]:
            unnamed += 1
        total_pts += len(coords)
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "name": meta["name"],
                "ref": meta["ref"],
                "route": meta["route"],
                "colour": meta["colour"],
                "operator": meta["operator"],
                "network": meta["network"],
            },
        })

    features.sort(key=lambda f: (f["properties"]["route"],
                                 f["properties"]["ref"] or f["properties"]["name"]))

    doc = {
        "city": args.city,
        "extract": args.extract,
        "bbox": [min_lat, min_lon, max_lat, max_lon],
        "coverage": {
            "routeRelations": len(c.routes),
            "routesFilteredOut": filtered,
            "routesByType": dict(sorted(c.counts.items())),
            "linesEmitted": len(features),
            "droppedNoGeometry": dropped_no_geom,
            "unnamedLines": unnamed,
            "namedLines": len(features) - unnamed,
            "totalVertices": total_pts,
            "distinctNames": len({f["properties"]["name"] for f in features
                                  if f["properties"]["name"]}),
            "withColour": sum(1 for f in features if f["properties"]["colour"]),
        },
        "lines": {"type": "FeatureCollection", "features": features},
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))

    cov = doc["coverage"]
    out = sys.stdout
    out.write(f"{args.city}: {cov['linesEmitted']} lines from {cov['routeRelations']} routes\n")
    out.write(f"  by type: {cov['routesByType']}\n")
    out.write(f"  named {cov['namedLines']}/{cov['linesEmitted']}  "
              f"colour {cov['withColour']}  vertices {cov['totalVertices']}  "
              f"dropped(no geom) {cov['droppedNoGeometry']}\n")


if __name__ == "__main__":
    main()