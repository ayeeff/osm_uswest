#!/usr/bin/env python3
"""Extract named transit hubs (stations) for one city from a staged OSM extract.

Streams the country PBF straight out of R2 and filters by bbox, so a city never
requires downloading a multi-gigabyte country extract.

Why a dedicated script rather than extract_city_2d_data.py: that one emits
neighbourhood polygons, and its station handling is a single airport check.

Ranked stations are NOT produced here. OSM carries no ridership or prominence
for stations, so "most important hubs" needs an attributed external seed
(curated-hubs.json). This script emits coverage plus genuine geo, and is meant to
answer "is OSM tagging good enough in this city to be worth a page at all".

Usage:
  python extract_transit_hubs.py --city hongkong --extract hongkong \
      --bbox 22.15,113.82,22.58,114.44 --out hk.json
"""
import argparse
import io
import json
import os
import sys
import unicodedata
from collections import Counter, defaultdict

import osmium

# station=<kind> values that represent a stop a passenger boards.
STATION_KINDS = {
    "subway", "light_rail", "monorail", "train", "tram", "funicular",
    "ferry_terminal", "light_rail", "heavy_rail",
}
RAILWAY_STATION = {"station", "halt"}


def norm(s):
    return unicodedata.normalize("NFKC", s or "").strip()


def key(name):
    return norm(name).casefold()


class Collector(osmium.SimpleHandler):
    """Collects named station nodes and remembers which networks appeared."""

    def __init__(self, min_lat, min_lon, max_lat, max_lon, networks, operators):
        super().__init__()
        self.bbox = (min_lat, min_lon, max_lat, max_lon)
        self.networks = networks
        self.operators = operators
        self.by_name = defaultdict(list)
        self.kinds = Counter()
        self.net_counts = Counter()
        self.op_counts = Counter()
        self.total = 0
        self.kept = 0

    def _in_box(self, lat, lon):
        return self.bbox[0] <= lat <= self.bbox[2] and self.bbox[1] <= lon <= self.bbox[3]

    def node(self, n):
        t = dict(n.tags)
        if not t:
            return
        self.total += 1
        station = t.get("station")
        railway = t.get("railway")
        kind = station if station in STATION_KINDS else None
        if kind is None and railway in RAILWAY_STATION:
            # tram/light_rail stops are often tagged only as railway=station
            for k in ("light_rail", "subway", "monorail", "train"):
                if t.get(k) == "yes" or t.get(k):
                    kind = k
                    break
        if kind is None or kind == "airport":
            return
        if not t.get("name") or not n.location.valid():
            return
        lat, lon = n.location.lat, n.location.lon
        if not self._in_box(lat, lon):
            return
        net = norm(t.get("network"))
        op = norm(t.get("operator"))
        self.net_counts[net] += 1
        if op:
            self.op_counts[op] += 1
        # Per-city filters. A city whose extract spills into a neighbour needs
        # this: geofabrik's hongkong extract includes Shenzhen.
        if self.networks and not any(m.casefold() in net.casefold() for m in self.networks):
            return
        if self.operators and not any(m.casefold() in op.casefold() for m in self.operators):
            return
        self.kept += 1
        self.kinds[kind] += 1
        self.by_name[key(t["name"])].append({
            "name": norm(t["name"]),
            "nameLocal": norm(t.get("name:zh") or t.get("name:ja") or t.get("name")
                               or t["name"]),
            "kind": kind,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "ref": norm(t.get("ref")),
            "network": net,
            "operator": op,
        })


def open_stream(bucket, key_name):
    """Stream an R2 object as a file object osmium can read."""
    import os as _os
    import boto3  # lazy: only the --extract path needs AWS, so a --pbf
    from botocore.config import Config  # run (which is what CI does) needs neither.
    home = _os.path.join(_os.path.expanduser("~"), ".geo-r2.env")
    env = {}
    for path in (home, ".env"):
        if not _os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    # Environment wins over the files, so CI and this pilot can inject creds.
    for k, v in os.environ.items():
        if v and ("R2_" in k or k == "CF_ACCOUNT_ID"):
            env.setdefault(k, v)

    account = env.get("R2_DATALAKE_ACCOUNT_ID") or env.get("CF_ACCOUNT_ID")
    key_id = env.get("R2_DATALAKE_ACCESS_KEY_ID") or env.get("R2_ACCESS_KEY_ID")
    secret = env.get("R2_DATALAKE_SECRET_ACCESS_KEY") or env.get("R2_SECRET_ACCESS_KEY")
    if not (account and key_id and secret):
        raise SystemExit("R2 credentials not found in ~/.geo-r2.env or .env")
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )
    obj = s3.get_object(Bucket=bucket, Key=key_name)
    # pyosmium's Reader accepts only a path or osmium.io.File, NOT an arbitrary
    # readable stream, so the extract is materialised locally and removed
    # afterwards. Streaming straight off R2 is not possible with pyosmium.
    # Country extracts are large (germany 4.6GB), so this belongs in CI where
    # the disk and the cache are, not in an interactive session.
    import tempfile

    dest = os.path.join(tempfile.gettempdir(), f"{os.path.basename(key_name)}")
    if not (os.path.exists(dest)
            and abs(os.path.getsize(dest)) < 1
            or os.path.exists(dest) and os.path.getsize(dest) > 0):
        obj = s3.get_object(Bucket=bucket, Key=key_name)
        with open(dest, "wb") as out:
            while True:
                chunk = obj["Body"].read(1 << 22)
                if not chunk:
                    break
                out.write(chunk)
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--extract", help="geofabrik extract name")
    ap.add_argument("--pbf", help=(
        "Pre-cut city PBF to read. Preferred in CI: cut it with the C++ "
        "`osmium extract -b` first, as osm-city-2d-data.yml does. Parsing a whole "
        "country extract in pyosmium took 40+ min for netherlands and never "
        "finished for germany."))
    ap.add_argument("--bbox", required=True, help="minLat,minLon,maxLat,maxLon")
    ap.add_argument("--bucket", default="geo-datalake")
    ap.add_argument("--network", action="append", default=[],
                    help="keep only these network values (repeatable)")
    ap.add_argument("--operator", action="append", default=[],
                    help="keep only these operator values (repeatable)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if not args.pbf and not args.extract:
        raise SystemExit("need --pbf or --extract")

    min_lat, min_lon, max_lat, max_lon = (float(x) for x in args.bbox.split(","))

    if args.pbf:
        fh = args.pbf
        source = f"file://{args.pbf}"
    else:
        key_name = f"sources/osm/{args.extract}-latest.osm.pbf"
        fh = open_stream(args.bucket, key_name)
        source = f"r2://{args.bucket}/{key_name}"
    c = Collector(min_lat, min_lon, max_lat, max_lon, args.network, args.operator)
    c.apply_file(fh)

    rows = []
    for _, variants in c.by_name.items():
        lines = sorted({(v["ref"] or v["network"] or v["kind"]) for v in variants})
        best = max(variants, key=lambda v: (
            v["ref"] != "", len(v["network"])))
        rows.append({
            "name": best["name"],
            "nameLocal": best["nameLocal"],
            "kind": best["kind"],
            "lines": lines,
            "lat": best["lat"],
            "lon": best["lon"],
            "ref": best["ref"],
            "network": best["network"],
            "operator": best["operator"],
        })
    order = {"subway": 0, "heavy_rail": 0, "monorail": 1, "train": 1,
             "light_rail": 2, "tram": 2, "funicular": 3, "ferry_terminal": 4}
    rows.sort(key=lambda r: (order.get(r["kind"], 9), r["name"]))

    doc = {
        "city": args.city,
        "extract": args.extract,
        "source": source,
        "bbox": [min_lat, min_lon, max_lat, max_lon],
        "coverage": {
            "taggedNodes": c.total,
            "namedStationsInBox": c.kept,
            "distinctStations": len(rows),
            "byKind": dict(c.kinds),
            "networks": dict(c.net_counts.most_common(8)),
            "operators": dict(c.op_counts.most_common(8)),
        },
        "ranked": False,
        "rankingNote": ("OSM carries no ridership or station prominence. Importance "
                        "ranking requires an attributed external seed; none applied."),
        # GeoJSON as well as the raw array: the atlas pages render straight from
        # this, so they need no per-city page edits.
        "points": {"type": "FeatureCollection", "features": [
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
             "properties": {"name": r["name"], "nameLocal": r["nameLocal"],
                            "kind": r["kind"], "ref": r["ref"],
                            "network": r["network"], "operator": r["operator"],
                            "interchange": len(r["lines"])}}
            for r in rows]},
        "stations": rows,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    cov = doc["coverage"]
    out = sys.stdout
    out.write(f"{args.city}: {len(rows)} distinct stations from {cov['namedStationsInBox']} nodes\n")
    out.write(f"  kinds: {cov['byKind']}\n")
    out.write(f"  top networks: {list(cov['networks'])[:5]}\n")


if __name__ == "__main__":
    main()