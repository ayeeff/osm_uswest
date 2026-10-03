#!/usr/bin/env python3
"""Extract named-highway street lists per city bbox from an OSM PBF file.

Single streaming pass: PBF stores all nodes before all ways, so named-highway
ways can be resolved against an in-memory index of the nodes that fall inside a
target city box. Memory stays bounded (only boxed nodes are retained) and the
expensive per-node work is a single global bbox reject.

usage: city-streets-pbf.py <pbf> <cities.json> <out.json> [max_per_city]
"""

import json
import sys
import time
import unicodedata
from bisect import bisect_right

import numpy as np
import osmium

ALLOWED = {
    "motorway": 0,
    "trunk": 1,
    "primary": 2,
    "secondary": 3,
    "tertiary": 4,
    "unclassified": 5,
    "residential": 6,
    "living_street": 7,
    "pedestrian": 8,
    "service": 9,
}

CLASS_LABEL = {
    "motorway": "Highway",
    "trunk": "Highway",
    "primary": "Avenue",
    "secondary": "Road",
    "tertiary": "Street",
    "unclassified": "Street",
    "residential": "Street",
    "living_street": "Street",
    "pedestrian": "Walk",
    "service": "Lane",
}

SUFFIXES = [
    ("motorway", "Highway"), ("highway", "Highway"), ("hwy", "Highway"),
    ("freeway", "Highway"), ("expressway", "Highway"),
    ("avenue", "Avenue"), ("ave", "Avenue"),
    ("boulevard", "Boulevard"), ("blvd", "Boulevard"),
    ("road", "Road"), ("rd", "Road"),
    ("drive", "Drive"), ("dr", "Drive"),
    ("street", "Street"), ("st", "Street"),
    ("lane", "Lane"), ("ln", "Lane"),
    ("court", "Court"), ("ct", "Court"),
    ("place", "Place"), ("pl", "Place"),
    ("crescent", "Crescent"), ("cres", "Crescent"),
    ("parkway", "Parkway"), ("pkwy", "Parkway"),
    ("square", "Square"), ("sq", "Square"),
    ("terrace", "Terrace"), ("walk", "Walk"),
]

CHUNK_NODES = 4_000_000


def canon(name):
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("'", "").replace("’", "").replace(".", "")
    return " ".join(s.split())


def label_for(name, cls):
    low = " " + canon(name).replace("-", " ") + " "
    for token, label in SUFFIXES:
        if " " + token + " " in low:
            return label
    return CLASS_LABEL.get(cls, "Street")


def half_degrees(pop):
    pop_m = max(float(pop or 0), 50_000) / 1_000_000
    half_lat = 0.115 + min(0.20, (pop_m ** 0.5) / 320.0)
    return half_lat, half_lat * 1.25


class Extractor(osmium.SimpleHandler):
    def __init__(self, boxes, want_geometry=True, max_points=60, max_runs=2):
        super().__init__()
        ordered = sorted(boxes, key=lambda b: b["min_lat"])
        self.state = {
            "boxes": ordered,
            "min_lats": [b["min_lat"] for b in ordered],
            "max_lats": [b["max_lat"] for b in ordered],
            "span": max((b["max_lat"] - b["min_lat"]) for b in ordered) if ordered else 0,
            "gmin_lat": min(b["min_lat"] for b in ordered),
            "gmax_lat": max(b["max_lat"] for b in ordered),
            "gmin_lng": min(b["min_lng"] for b in ordered),
            "gmax_lng": max(b["max_lng"] for b in ordered),
            "id_chunks": [],
            "coord_chunks": [],
            "buf_ids": np.empty(CHUNK_NODES, dtype=np.int64),
            "buf_coord": np.empty(CHUNK_NODES, dtype=np.int64),
            "buf_n": 0,
            "sorted_ids": None,
            "sorted_coord": None,
            "finalized": False,
            "hits": {},
            "node_count": 0,
            "boxed_nodes": 0,
            "named_ways": 0,
            "ways_with_geom": 0,
            "t0": time.time(),
        }

        st = self.state
        boxes_l = ordered
        min_lats = st["min_lats"]
        max_lats = st["max_lats"]
        span = st["span"]
        gmin_lat = st["gmin_lat"]
        gmax_lat = st["gmax_lat"]
        gmin_lng = st["gmin_lng"]
        gmax_lng = st["gmax_lng"]
        buf_ids = st["buf_ids"]
        buf_coord = st["buf_coord"]

        def node(n):
            st["node_count"] += 1
            lat = n.location.lat
            if lat < gmin_lat or lat > gmax_lat:
                return
            lng = n.location.lon
            if lng < gmin_lng or lng > gmax_lng:
                return
            lo_bound = lat - span
            i = bisect_right(min_lats, lat) - 1
            inside = None
            while i >= 0 and min_lats[i] >= lo_bound:
                if max_lats[i] >= lat:
                    b = boxes_l[i]
                    if b["min_lng"] <= lng <= b["max_lng"]:
                        inside = b
                        break
                i -= 1
            if inside is None:
                return
            k = st["buf_n"]
            if k >= CHUNK_NODES:
                st["id_chunks"].append(buf_ids.copy())
                st["coord_chunks"].append(buf_coord.copy())
                k = 0
            buf_ids[k] = n.id
            buf_coord[k] = (int(round(lat * 1e7)) << 32) | (int(round(lng * 1e7)) & 0xFFFFFFFF)
            st["buf_n"] = k + 1
            st["boxed_nodes"] += 1

        self.node = node

        finalize = self.finalize_nodes
        lookup = self.lookup
        allowed = ALLOWED

        def in_box(pt, bx):
            return (
                bx["min_lat"] <= pt[0] <= bx["max_lat"]
                and bx["min_lng"] <= pt[1] <= bx["max_lng"]
            )

        def clip_edge(prev, nxt, bx):
            """Largest t in [0,1] with lerp(prev,nxt,t) still inside the box.

            Called only when prev is inside and nxt is outside, so the line does
            cross the boundary; bisection converges fast and is exact enough at
            1e-7 degrees (~1 cm).
            """
            lo, hi = 0.0, 1.0
            for _ in range(24):
                mid = (lo + hi) / 2.0
                lat = prev[0] + (nxt[0] - prev[0]) * mid
                lng = prev[1] + (nxt[1] - prev[1]) * mid
                if bx["min_lat"] <= lat <= bx["max_lat"] and bx["min_lng"] <= lng <= bx["max_lng"]:
                    lo = mid
                else:
                    hi = mid
            t = lo
            return (
                prev[0] + (nxt[0] - prev[0]) * t,
                prev[1] + (nxt[1] - prev[1]) * t,
            )

        def thin(run):
            """Evenly subsample to max_points, always keeping both ends."""
            if len(run) <= max_points:
                return run
            step = (len(run) - 1) / float(max_points - 1)
            out = [run[int(round(i * step))] for i in range(max_points)]
            out[0] = run[0]
            out[-1] = run[-1]
            return out

        def polyline(refs, bx):
            """Resolve a way's node refs into [lng,lat] runs clipped to the box.

            Only nodes inside SOME city box are retained in the index, so a way
            that leaves the box yields None for its outside nodes. Each maximal
            in-box run becomes one output run, with the boundary crossing
            interpolated so the line still reaches the edge of the city.
            """
            runs = []
            cur = []
            for ref in refs:
                pt = lookup(ref)
                if pt is not None and in_box(pt, bx):
                    cur.append((pt[1], pt[0]))
                    continue
                if cur:
                    if pt is not None:
                        lat, lng = clip_edge((cur[-1][1], cur[-1][0]), pt, bx)
                        cur.append((lng, lat))
                    if len(cur) >= 2:
                        runs.append(cur)
                    cur = []
                if len(runs) >= max_runs:
                    break
            if cur and len(cur) >= 2 and len(runs) < max_runs:
                runs.append(cur)
            runs = [thin([(round(x, 5), round(y, 5)) for x, y in r]) for r in runs]
            runs = [r for r in runs if len(r) >= 2]
            if not runs:
                return None
            runs.sort(key=len, reverse=True)
            return runs[:max_runs]

        def way(w):
            finalize()
            tags = w.tags
            cls = tags.get("highway")
            if cls not in allowed:
                return
            name = tags.get("name")
            en = tags.get("name:en")
            if not name and not en:
                return
            st["named_ways"] += 1
            nodes = w.nodes
            n_nodes = len(nodes)
            if not n_nodes:
                return
            # Sample up to 9 refs: long arterials often have both endpoints
            # outside the city box while running straight through it.
            if n_nodes <= 9:
                probes = [nodes[i].ref for i in range(n_nodes)]
            else:
                step = (n_nodes - 1) / 8.0
                probes = [nodes[int(round(i * step))].ref for i in range(9)]
            hit = None
            hit_ref = None
            for ref in probes:
                pt = lookup(ref)
                if pt is None:
                    continue
                for b in boxes_l:
                    if b["min_lat"] <= pt[0] <= b["max_lat"] and b["min_lng"] <= pt[1] <= b["max_lng"]:
                        hit = (b, pt)
                        hit_ref = ref
                        break
                if hit:
                    break
            if not hit:
                return
            b, seed = hit
            # Only ways that actually land in a city box pay for the full node
            # walk below, so the expensive geometry pass runs on a small subset.
            runs = polyline([nd.ref for nd in nodes], b) if want_geometry else None
            display = name or en
            key = canon(display or en) + "@" + f"{seed[0]:.2f},{seed[1]:.2f}"
            bucket = st["hits"].setdefault(b["slug"], {})
            prev = bucket.get(key)
            prio = allowed[cls]
            if prev and prev["prio"] <= prio:
                return
            row = {
                "n": display,
                "raw": en or display,
                "t": label_for(display, cls),
                "la": round(seed[0], 6),
                "lo": round(seed[1], 6),
                "p": 0,
                "cls": cls,
                "prio": prio,
            }
            if runs:
                row["g"] = runs
                st["ways_with_geom"] += 1
            bucket[key] = row

        self.way = way

    def finalize_nodes(self):
        st = self.state
        if st["finalized"]:
            return
        st["finalized"] = True
        parts_i = list(st["id_chunks"])
        parts_c = list(st["coord_chunks"])
        if st["buf_n"]:
            parts_i.append(st["buf_ids"][: st["buf_n"]].copy())
            parts_c.append(st["buf_coord"][: st["buf_n"]].copy())
        if not parts_i:
            st["sorted_ids"] = np.empty(0, dtype=np.int64)
            st["sorted_coord"] = np.empty(0, dtype=np.int64)
            return
        ids = np.concatenate(parts_i)
        coord = np.concatenate(parts_c)
        order = np.argsort(ids, kind="stable")
        st["sorted_ids"] = ids[order]
        st["sorted_coord"] = coord[order]

    def lookup(self, ref):
        ids = self.state["sorted_ids"]
        if ids is None or ids.size == 0:
            return None
        i = int(np.searchsorted(ids, ref))
        if i >= ids.size or ids[i] != ref:
            return None
        v = int(self.state["sorted_coord"][i])
        # High 32 bits hold the signed latitude (arithmetic shift restores it);
        # low 32 bits hold the longitude as unsigned, so re-sign it.
        lat = (v >> 32) / 1e7
        lng_i = v & 0xFFFFFFFF
        if lng_i > 1800000000:
            lng_i -= 4294967296
        return (lat, lng_i / 1e7)


def main():
    pbf_path, cities_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    max_per_city = int(sys.argv[4]) if len(sys.argv) > 4 else 320
    # 5th arg caps points per geometry run; 0 disables geometry emission
    # entirely and restores the original seed-point-only output.
    max_points = int(sys.argv[5]) if len(sys.argv) > 5 else 60
    with open(cities_path, "r", encoding="utf-8") as fh:
        cities = json.load(fh)

    region = None
    try:
        reader = osmium.io.Reader(pbf_path)
        bbox = reader.header().bbox()
        region = (bbox.min_lon, bbox.min_lat, bbox.max_lon, bbox.max_lat)
    except Exception:
        region = None
    print(f"[extract] region bbox: {region}", flush=True)

    boxes = []
    for c in cities:
        lat = float(c["lat"])
        lng = float(c["lng"])
        half_lat, half_lng = half_degrees(c.get("pop") or 0)
        if region and not (
            region[0] - 1 <= lng <= region[2] + 1 and region[1] - 1 <= lat <= region[3] + 1
        ):
            continue
        boxes.append({
            "slug": c["slug"],
            "name": c.get("name") or c["slug"],
            "min_lat": lat - half_lat,
            "max_lat": lat + half_lat,
            "min_lng": lng - half_lng,
            "max_lng": lng + half_lng,
        })
    print(f"[extract] {len(boxes)}/{len(cities)} candidate cities", flush=True)
    print(f"[extract] geometry={'on' if max_points > 0 else 'off'} max_points={max_points}", flush=True)
    if not boxes:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({"region": region, "cities": {}}, fh)
        return

    h = Extractor(boxes, want_geometry=max_points > 0, max_points=max(2, max_points))
    h.apply_file(pbf_path, locations=False)

    hits = h.state["hits"]
    out = {"region": region, "cities": {}}
    for b in boxes:
        bucket = hits.get(b["slug"]) or {}
        rows = sorted(bucket.values(), key=lambda r: (r["prio"], r["n"] or ""))
        for r in rows:
            r.pop("prio", None)
        out["cities"][b["slug"]] = {
            "name": b["name"],
            "streets": rows[:max_per_city],
        }

    st = h.state
    elapsed = time.time() - st["t0"]
    print(
        f"[extract] nodes={st['node_count']} boxed={st['boxed_nodes']} "
        f"named_ways={st['named_ways']} with_geom={st['ways_with_geom']} "
        f"elapsed={elapsed:.0f}s "
        f"rate={st['node_count'] / max(elapsed, 1):,.0f} nodes/s",
        flush=True,
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)


if __name__ == "__main__":
    main()
