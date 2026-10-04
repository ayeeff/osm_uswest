#!/usr/bin/env python3
"""
Extract REAL OSM road geometry for a city's popular streets from the country
PBF already staged in geo-datalake.

    python scripts/extract_street_geoms.py \
        --slug hongkong --extract hongkong \
        --pbf work/city.osm.pbf \
        --streets work/streets.json \
        --out work/out/<slug>/street-geoms.json

Why this exists. /api/street-geoms.json used to source geometry live from public
Overpass mirrors, which AGENTS.md bans outright ("CRITICAL - No public Overpass
API. Ever."). Where it had nothing cached it returned count:0 and the map drew
a stand-in: Hong Kong's 30 popular streets all rendered as identical short
diagonal stubs, because there was no geometry to draw. Melbourne looked correct
only because its 329 streets had already been cached from an earlier Overpass
run. That is a cache lottery, not a pipeline.

geo-datalake holds every country's extract under sources/osm/<cc>-latest.osm.pbf,
and the city pipeline has already cut the city bbox to work/city.osm.pbf with
`-s complete_ways`. So the geometry can come from the same sanctioned source the
boundaries already come from, in the same pass, with no new download.

Output is byte-compatible with the existing /api/street-geoms.json shape, keyed
by canonical street name, because the harness looks up
[streetKey, canonicalName, canonicalRaw] in that order - a name-only key is
enough.

canonical_street_key() is a deliberate port of canonicalStreetKey() in
src/pages/api/street-geoms.json.ts. If the JS side changes, change both.
"""

import argparse
import json
import math
import os
import re
import sys
import unicodedata
import urllib.request

import osmium
from osmium.osm import Way

# Same table as the JS side, and the same order.
ABBREVIATIONS = [
    (r"\bave\b", "avenue"),
    (r"\brd\b", "road"),
    (r"\bst\b", "street"),
    (r"\bdr\b", "drive"),
    (r"\bblvd\b", "boulevard"),
    (r"\bln\b", "lane"),
    (r"\bct\b", "court"),
    (r"\bhwy\b", "highway"),
    (r"\bcres\b", "crescent"),
    (r"\bpl\b", "place"),
    (r"\bwy\b", "way"),
]

MAX_COORDS_PER_STREET = 600
ROUND = 6

# Keep only way fragments near the street's own seed point. Without this, a name
# like "Hauptstrasse" collects every fragment with that name across the whole
# city and the street ends up scattered over the map - which is also what makes
# the file large. The old Overpass implementation pinned its per-street query to
# a tiny bbox for the same reason; this is the offline equivalent.
SEED_RADIUS_M = 2500.0

# Diagnostic sweep radius, deliberately wider than SEED_RADIUS_M: a name that
# missed at 2.5 km may well exist at 4 km, and that is exactly the distinction
# the report exists to draw between "named differently" and "not there".
DIAG_RADIUS_M = 4000.0

# Slugs where the page name and the city-streets index name differ. Verified by
# querying the index: washingtondc, washington and newyorkcity all 404, while dc,
# ny, sf and la all answer with hundreds of streets.
#
# Kept explicit and one-directional. Each entry was checked against the live
# index, and adding an alias that does not exist would silently resolve some other
# city's streets onto this page - the precise failure this pipeline exists to
# prevent.
CITY_STREET_ALIASES = {
    "washingtondc": ["dc", "washington"],
    "newyorkcity": ["ny", "newyork"],
    "sanfrancisco": ["sf"],
    "losangeles": ["la"],
    "mexicocity": ["mexico-city"],
    "hcmc": ["hochiminhcity"],
    "kolkata": ["calcutta"],
    "mumbai": ["bombay"],
    "chennai": ["madras"],
    "bengaluru": ["bangalore"],
    "beijing": ["peking"],
    "saopaulo": ["sao-paulo"],
    "riyadh": ["riyadh-city"],
}

# Address datasets and OSM spell the same street differently often enough to
# matter: "Tiergartenstrasse" vs "TiergartenstraÃƒÆ’Ã†â€™Ãƒâ€¦Ã‚Â¸e", "GÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¶teborgsgatan" vs
# "Goteborgsgatan". Folding these before matching recovered most of the misses.
TRANSLITERATE = {
    "ÃƒÆ’Ã†â€™Ãƒâ€¦Ã‚Â¸": "ss", "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¦": "ae", "ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ": "ae", "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¸": "o", "ÃƒÆ’Ã†â€™Ãƒâ€¹Ã…â€œ": "o",
    "ÃƒÆ’Ã¢â‚¬Å¾ÃƒÂ¢Ã¢â€šÂ¬Ã‹Å“": "d", "ÃƒÆ’Ã¢â‚¬Å¾Ãƒâ€šÃ‚Â": "d", "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â°": "d", "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â": "d", "ÃƒÆ’Ã¢â‚¬Â¦ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡": "l", "ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â": "l",
    "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¾": "th", "ÃƒÆ’Ã†â€™Ãƒâ€¦Ã‚Â¾": "th", "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢": "'", "'": "'",
}


def canonical_street_key(name):
    if not name:
        return ""
    k = unicodedata.normalize("NFD", str(name).lower())
    k = "".join(ch for ch in k if not unicodedata.combining(ch))
    for src, dst in TRANSLITERATE.items():
        k = k.replace(src, dst)
    k = re.sub(r"['ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢.]", "", k)
    k = re.sub(r"\s+", " ", k).strip()
    for pattern, full in ABBREVIATIONS:
        k = re.sub(pattern, full, k)
    return k


def simplify(coords, min_m=2.0):
    """
    Drop consecutive points closer than ~2 m.

    OSM ways carry a node at every corner, kerb and driveway entrance, so the
    raw geometry is several times larger than the shape needs. Berlin's 71
    streets came to 6.6 KB each before this.
    """
    if len(coords) < 3:
        return coords
    out = [coords[0]]
    lat0 = coords[0][1]
    mx = 111320.0 * max(0.05, math.cos(math.radians(lat0)))
    my = 110574.0
    for pt in coords[1:]:
        prev = out[-1]
        dx = (pt[0] - prev[0]) * mx
        dy = (pt[1] - prev[1]) * my
        if (dx * dx + dy * dy) >= min_m * min_m:
            out.append(pt)
    if len(out) >= 2 and out[0] != out[-1]:
        out.append(out[0])
    return out


def load_street_names(source, limit):
    """
    Pull the streets to resolve out of a city-streets payload. Accepts either an
    already-parsed document or a path, because the caller now resolves the
    document through the alias fallback and the path would be the stale,
    mismatched-slug file that prompted it.

    Returns (seeds, names) where each seed carries the canonical key, the display
    name and the lat/lon the dataset placed it at. Best-known streets first,
    deduplicated on the canonical key.
    """
    if isinstance(source, (dict, list)):
        doc = source
    else:
        with open(source, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    rows = doc.get("streets") if isinstance(doc, dict) else doc
    if not rows:
        return [], []
    scored = []
    for row in rows:
        name = row.get("n") or row.get("name") or row.get("raw")
        if not name:
            continue
        la, lo = row.get("la"), row.get("lo")
        if not isinstance(la, (int, float)) or not isinstance(lo, (int, float)):
            continue
        scored.append((canonical_street_key(name), name, int(row.get("p") or 0), la, lo))
    scored.sort(key=lambda t: -t[2])
    seen = set()
    seeds = []
    for key, name, _p, la, lo in scored:
        if key in seen:
            continue
        seen.add(key)
        seeds.append((key, name, la, lo))
    if limit:
        seeds = seeds[:limit]
    return seeds, [s[1] for s in seeds]


class WayCollector(osmium.SimpleHandler):
    def __init__(self, seeds):
        super().__init__()
        self.seeds = {k: (la, lo) for k, _n, la, lo in seeds}
        self.display = {k: n for k, n, _la, _lo in seeds}
        self.wanted = set(self.seeds)
        self.geoms = {}
        self.matched = set()
        # Index of the seed keys with leading words dropped, so an OSM name can
        # find a seed whose key merely has extra words in front.
        #
        # This is the other half of lookup_keys(). That one shortens the OSM
        # name; this shortens the seed. Hong Kong needs this side: the street
        # list says "The Peak Road" and OSM says "Peak Road", so the OSM name
        # has nothing to drop and the seed is the one carrying the extra word.
        # Shortening both sides in isolation misses the pair, which is what
        # happened - the first attempt took Hong Kong 0 -> 7 and then stalled.
        self.by_short = {}
        for key, (lat, lon) in self.seeds.items():
            words = key.split(" ")
            for cut in range(1, len(words)):
                self.by_short.setdefault(" ".join(words[cut:]), (key, (lat, lon)))

    def way(self, w):
        tags = w.tags
        if not tags:
            return
        # Every name a way is known by, Latin script first.
        #
        # Hong Kong matched 0 of 17 streets reading only `name`, which looks
        # impossible: Nathan Road and Shanghai Street are among the busiest
        # roads in the territory and are plainly in the extract. The reason is
        # that Hong Kong is mapped bilingually. Most of its roads carry the
        # Chinese name in `name` and the romanised name in `name:en`, so an
        # English street list compared against `name` never meets its own
        # roads. Berlin matched 63 of 93 on `name` alone for the opposite
        # reason - Germany has no second name to prefer.
        #
        # `name:latin` and `int_name` cover the same ground elsewhere: Taiwan
        # and parts of Central Asia and the Balkans put the Latin form in
        # `name:latin`, and `int_name` is the international fallback. Checking
        # all of them costs three dict lookups and makes the city work by tag
        # convention rather than by which languages a country happens to map.
        for tag in ("name", "name:en", "name:latin", "int_name"):
            raw = tags.get(tag)
            if not raw:
                continue
            for key, seed in self.lookup_keys(raw):
                if self._take(key, seed, w):
                    return

    def lookup_keys(self, raw):
        """Yield (canonical key, seed) candidates for one OSM name.

        Exact match only was not enough. Hong Kong's list says "The Peak Road"
        while OSM's own `name:en` says "Peak Road" - the same road, described by
        an address database that keeps the definite article and an OSM mapper
        who did not. Requiring equality missed it by one word.

        So after the exact key fails, retry dropping leading words one at a
        time. The seed requirement still pins each match to within 2.5 km of the
        street's own point, which is what keeps this from becoming the fuzzy
        substring matching that makes "Hauptstrasse" collect fragments across a
        whole city: dropping a word is a bounded, explainable edit, and every
        candidate is still rejected unless a road of that name runs near the
        seed. A name with two or three words is not ambiguous once the geography
        is applied.
        """
        key = canonical_street_key(raw)
        seed = self.seeds.get(key)
        if seed is not None:
            yield key, seed
            return
        # The OSM name is shorter than the seed's: "Peak Road" should find the
        # seed "The Peak Road". Yield the SEED's key, not the shortened one, so
        # the geometry lands under the name the client looks up.
        found = self.by_short.get(key)
        if found is not None:
            yield found[0], found[1]
            return
        words = key.split(" ")
        for cut in range(1, len(words)):
            shorter = " ".join(words[cut:])
            s = self.seeds.get(shorter)
            if s is not None:
                yield shorter, s
                return
            found = self.by_short.get(shorter)
            if found is not None:
                yield found[0], found[1]
                return

    def _take(self, key, seed, w):
        coords = []
        for nd in w.nodes:
            if nd.location.valid():
                coords.append([round(nd.location.lon, ROUND), round(nd.location.lat, ROUND)])
        if len(coords) < 2:
            return False
        seed_lat, seed_lon = seed
        mx = 111320.0 * max(0.05, math.cos(math.radians(seed_lat)))
        # Distance to the CLOSEST point of the way, not to its midpoint node.
        #
        # Measuring to the midpoint let a long way qualify on the strength of
        # one node being near the seed: Berlin's Pariser Platz matched geometry
        # whose nearest point is 2.2 km from the seed, because some mid-node
        # happened to fall inside the radius. The seed is a point of interest
        # ON the street, so the street has to come to the point. This also makes
        # long fragmented roads behave like short ones instead of matching on
        # whichever fragment happens to be nearest.
        best = float("inf")
        for pt in coords:
            dx = (pt[0] - seed_lon) * mx
            dy = (pt[1] - seed_lat) * 110574.0
            d2 = dx * dx + dy * dy
            if d2 < best:
                best = d2
                if best <= 0.0:
                    break
        if best > SEED_RADIUS_M * SEED_RADIUS_M:
            return False
        coords = simplify(coords)
        if len(coords) < 2:
            return False
        self.matched.add(key)
        if key not in self.geoms:
            self.geoms[key] = []
        if len(self.geoms[key]) < MAX_COORDS_PER_STREET:
            self.geoms[key].append(coords)
        return True


class DiagnoseCollector(osmium.SimpleHandler):
    """Report what OSM actually calls the roads a city failed to match.

    A miss is ambiguous on its own. "The Peak Road" not matching could mean it
    is unmapped, named only in Chinese, spelled differently, or mapped several
    kilometres from the seed. Guessing between those is how a street ends up
    with invented geometry, so this collects the ground truth instead: for every
    unresolved seed, the names of real named roads within DIAG_RADIUS_M, and
    which tag carried each name.

    That distinction matters more than it looks. Hong Kong's 10 rural misses
    (Ting Kok, The Peak Road, Repulse Bay Road) sit in exactly the territory
    where `name` is Chinese and `name:en` romanised, so the fix that took the
    city from 0 to 7 was correct but not sufficient - and only the PBF knows
    what is actually there.
    """

    def __init__(self, seeds, resolved):
        super().__init__()
        self.pending = [(k, la, lo) for (k, _n, la, lo) in seeds if k not in resolved]
        self.seeds = {(la, lo): k for k, la, lo in self.pending}
        self.found = {k: {} for k, _la, _lo in self.pending}

    def way(self, w):
        if not self.pending:
            return
        tags = w.tags
        if not tags:
            return
        names = {t: tags.get(t) for t in ("name", "name:en", "name:latin", "int_name")}
        names = {t: v for t, v in names.items() if v}
        if not names:
            return
        coords = []
        for nd in w.nodes:
            if nd.location.valid():
                coords.append((nd.location.lon, nd.location.lat))
        if len(coords) < 2:
            return
        lon, lat = coords[len(coords) // 2]
        for skey, slat, slon in self.pending:
            mx = 111320.0 * max(0.05, math.cos(math.radians(slat)))
            dx = (lon - slon) * mx
            dy = (lat - slat) * 110574.0
            d = math.hypot(dx, dy)
            if d > DIAG_RADIUS_M:
                continue
            for tag, val in names.items():
                self.found[skey].setdefault(str(val), []).append(
                    {"tag": tag, "m": int(d)}
                )

    def report(self):
        out = {}
        for key, entries in self.found.items():
            rows = []
            for name, locs in entries.items():
                tag = min(locs, key=lambda r: r["m"])["tag"]
                near = min(r["m"] for r in locs)
                rows.append({"osmName": name, "viaTag": tag, "nearestM": near})
            rows.sort(key=lambda r: r["nearestM"])
            out[key] = rows[:4]
        return out


def fetch_street_payload(slug, explicit=None, timeout=60):
    """The list of streets to resolve. Prefers a local file, else the public API.

    The city-streets index does not always use the same slug as the page. The
    Washington DC page is washingtondc-property-atlas while the index calls it
    "dc", so a single-slug lookup 404s and the city resolves nothing - reported as
    "no street names to resolve", which reads like an empty dataset rather than a
    wrong key. Same shape for ny/newyorkcity, sf/sanfrancisco, la/losangeles.

    So when the exact slug misses, try the documented aliases for that city.
    Aliases are explicit rather than generated: guessing would risk resolving one
    city's streets onto another's page, which is the failure mode this pipeline
    exists to prevent.
    """
    if explicit:
        with open(explicit, encoding="utf-8-sig") as fh:
            preloaded = json.load(fh)
        if preloaded.get("streets"):
            return preloaded
        # An explicit but EMPTY payload is a miss, not an answer.
        #
        # The workflow pre-fetches the list with curl and passes it as
        # --streets, so the 404 for a mismatched slug becomes a literal "{}" on
        # disk before this function is ever called. Returning it made the alias
        # table unreachable and the city silently produced nothing. Falling
        # through re-tries the API and gets the aliases.
        print(json.dumps({"note": "prefetched street list was empty, retrying",
                          "slug": slug}))

    candidates = [slug] + list(CITY_STREET_ALIASES.get(slug, []))
    last_error = None
    for cand in candidates:
        url = ("https://preview-geo-astro-site.foodstarmelbourne.workers.dev"
               f"/api/city-streets.json?city={cand}")
        req = urllib.request.Request(
            url, headers={"User-Agent": "ayeeff/geo-atlas-2d/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # 404 for an unknown slug is the expected miss
            last_error = exc
            continue
        if payload.get("streets"):
            if cand != slug:
                print(json.dumps({"note": "street list resolved via alias",
                                  "asked": slug, "used": cand}))
            return payload
        # A 200 with an empty list is a real answer, but keep looking in case an
        # alias does have data.
        last_error = "empty street list for %s" % cand

    if last_error is not None:
        print(json.dumps({"note": "no street list found", "slug": slug,
                          "tried": candidates, "detail": str(last_error)[:120]}))
    return {"streets": []}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--streets", help="city-streets JSON; fetched from the API if omitted")
    ap.add_argument("--limit", type=int, default=4000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--diagnose", action="store_true",
                    help="also report the real OSM names near any street that did not match")
    args = ap.parse_args()

    payload = fetch_street_payload(args.slug, args.streets)
    # The resolved payload, not args.streets: fetch_street_payload may have
    # replaced a stale prefetched file with an aliased fetch, and passing the
    # path back would re-read the empty file the fallback just rejected.
    seeds, names = load_street_names(payload, args.limit)
    if not seeds:
        print(json.dumps({"slug": args.slug, "count": 0, "geoms": {},
                          "note": "no street names to resolve"}))
        return 0

    collector = WayCollector(seeds)
    collector.apply_file(args.pbf, locations=True)

    diagnosis = None
    if args.diagnose:
        diag = DiagnoseCollector(seeds, collector.matched)
        diag.apply_file(args.pbf, locations=True)
        diagnosis = diag.report()

    geoms = {}
    for key, lines in collector.geoms.items():
        geoms[key] = {"n": collector.display.get(key, key), "lines": lines}

    out = {
        "slug": args.slug,
        "v": 1,
        "generated": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                      .isoformat().replace("+00:00", "Z"),
        "source": "geo-datalake country pbf (no overpass)",
        "wanted": len(seeds),
        "count": len(geoms),
        "geoms": geoms,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))

    missing = [n for k, n, _la, _lo in seeds if k not in geoms]
    size = os.path.getsize(args.out)
    summary = {
        "slug": args.slug,
        "wanted": len(seeds),
        "matched": len(geoms),
        "missing": len(missing),
        "bytes": size,
        "sampleMissing": missing[:5],
    }
    if diagnosis is not None:
        summary["diagnosis"] = {
            collector.display.get(k, k): v for k, v in diagnosis.items() if v
        }
        summary["unexplained"] = [
            collector.display.get(k, k) for k, v in diagnosis.items() if not v
        ]
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
