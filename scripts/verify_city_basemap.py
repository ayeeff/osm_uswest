#!/usr/bin/env python3
"""
Check that the served globe/basemaps/<slug>.pmtiles really is an extract of
<slug>.

    python scripts/verify_city_basemap.py --slug berlin \
        --bbox 12.66124,52.07137,14.14186,52.97227 [--url ...]

This exists because of a real failure. berlin.pmtiles advertised a `boundaries`
layer in its TileJSON and had a plausible-looking header, but it is not an
extract of Berlin at all: its bbox is 88.55 degrees away, in New Jersey. Every
tile request over Berlin came back empty, so the 2D map rendered an empty
basemap with no error anywhere. Nothing caught it because the archive is
internally consistent — a valid PMTiles v3 file, correct magic, a real tile
directory — just of the wrong place.

The only reliable check is comparing the archive's own declared extent against
the city we asked for. Done with an HTTP range request for the 127-byte header,
so it costs a few hundred bytes and no dependency on the pmtiles CLI.

Also explains the misleading `boundaries` layer: vector_layers comes from the
source build's global metadata, so every city archive advertises the same layer
list whether or not it contains any boundary geometry. Its presence proves
nothing about the contents.
"""

import argparse
import json
import struct
import sys
import urllib.request

SPEC_MAGIC = b"PMTiles"
# The fixed part of a v3 header is 127 bytes and ends at max_lat. centre_zoom /
# centre_lon / centre_lat are NOT fixed fields: they live in the varint-encoded
# optional section that follows, so read well past 127 to reach them.
HEADER_BYTES = 255
VARINT_FIELDS = {
    4: "numAddressedTiles", 5: "numTileEntries", 6: "numTileContents",
    7: "clustered", 8: "internalCompression", 9: "tileCompression",
    13: "minZoom", 14: "maxZoom",
    15: "minLon", 16: "minLat", 17: "maxLon", 18: "maxLat",
    19: "centerZoom", 20: "centerLon", 21: "centerLat",
}
# lon/lat are stored scaled by 1e7.
VARINT_SCALED = {15: 1e7, 16: 1e7, 17: 1e7, 18: 1e7, 20: 1e7, 21: 1e7}
# The site worker 403s requests that do not look like a browser or a declared
# client, so say who we are rather than sending urllib's default.
USER_AGENT = "ayeeff/geo-atlas-2d (basemap verifier; +https://github.com/ayeeff/astrogl)"


def read_varint(blob, pos):
    result = 0
    shift = 0
    while True:
        if pos >= len(blob):
            raise ValueError("truncated varint")
        byte = blob[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def fetch_header(url, timeout=30):
    """Read the PMTiles v3 header with a range request."""
    req = urllib.request.Request(
        url,
        headers={"Range": f"bytes=0-{HEADER_BYTES - 1}", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_header(blob):
    """
    PMTiles v3 header. Fixed fields first, then a varint-encoded section of
    optional <key, value> pairs.
    """
    if not blob.startswith(SPEC_MAGIC):
        raise ValueError("not a PMTiles file (bad magic)")
    version = blob[7]
    if version != 3:
        raise ValueError(f"unsupported PMTiles spec version {version}")

    root_dir_off, root_dir_len = struct.unpack_from("<QQ", blob, 8)
    meta_off, meta_len = struct.unpack_from("<QQ", blob, 24)
    leaf_off, leaf_len = struct.unpack_from("<QQ", blob, 40)
    tile_off, tile_len = struct.unpack_from("<QQ", blob, 56)
    num_addressed, num_entries, num_contents = struct.unpack_from("<QQQ", blob, 72)
    clustered = blob[96]
    internal_compression, tile_compression = blob[97], blob[98]
    tile_type = blob[99]
    min_zoom, max_zoom = blob[100], blob[101]
    # These four ARE fixed fields (bytes 102-117). Only centre_* is varint.
    min_lon, min_lat, max_lon, max_lat = struct.unpack_from("<iiii", blob, 102)

    out = {
        "rootDirOffset": root_dir_off, "rootDirLength": root_dir_len,
        "metadataOffset": meta_off, "metadataLength": meta_len,
        "leafDirOffset": leaf_off, "leafDirLength": leaf_len,
        "tileDataOffset": tile_off, "tileDataLength": tile_len,
        "numAddressedTiles": num_addressed, "numTileEntries": num_entries,
        "numTileContents": num_contents, "clustered": bool(clustered),
        "internalCompression": internal_compression, "tileCompression": tile_compression,
        "tileType": tile_type, "minZoom": min_zoom, "maxZoom": max_zoom,
        "minLon": min_lon / 1e7, "minLat": min_lat / 1e7,
        "maxLon": max_lon / 1e7, "maxLat": max_lat / 1e7,
    }

    # Walk the optional varint section and let it override/extend the fixed
    # fields. This is where centre_lon / centre_lat actually live.
    pos = 127
    while pos < len(blob):
        try:
            key, pos = read_varint(blob, pos)
            value, pos = read_varint(blob, pos)
        except ValueError:
            break
        name = VARINT_FIELDS.get(key)
        if name:
            out[name] = value / VARINT_SCALED[key] if key in VARINT_SCALED else value
        if key == 0:
            break

    for name in ("minLon", "minLat", "maxLon", "maxLat"):
        out.setdefault(name, 0.0)
    out.setdefault("centerLon", (out["minLon"] + out["maxLon"]) / 2)
    out.setdefault("centerLat", (out["minLat"] + out["maxLat"]) / 2)
    return out


def overlaps(a, b, slack=0.0):
    return not (
        a[2] < b[0] - slack or a[0] > b[2] + slack
        or a[3] < b[1] - slack or a[1] > b[3] + slack
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--bbox", required=True, help="minLng,minLat,maxLng,maxLat")
    ap.add_argument("--url", help="override the archive URL")
    ap.add_argument("--center-only", action="store_true",
                    help="warn instead of failing")
    args = ap.parse_args()

    url = args.url or f"https://preview-geo-astro-site.foodstarmelbourne.workers.dev/basemaps/{args.slug}.pmtiles"
    want = [float(v) for v in args.bbox.split(",")]
    if len(want) != 4:
        print("::error::--bbox needs minLng,minLat,maxLng,maxLat")
        return 2

    try:
        head = parse_header(fetch_header(url))
    except Exception as exc:                      # noqa: BLE001
        print(f"::error::{args.slug}: could not read {url}: {exc}")
        return 1

    have = [head["minLon"], head["minLat"], head["maxLon"], head["maxLat"]]
    want_centre = ((want[0] + want[2]) / 2, (want[1] + want[3]) / 2)
    off_by = max(abs(head["centerLon"] - want_centre[0]), abs(head["centerLat"] - want_centre[1]))

    print(f"  {args.slug}: centre=({head['centerLon']:.3f}, {head['centerLat']:.3f}) "
          f"tiles={head['numAddressedTiles']} data={head['tileDataLength'] / 1e6:.1f}MB "
          f"z{head['minZoom']}-{head['maxZoom']}")
    print(f"    archive bbox [{', '.join(f'{v:.3f}' for v in have)}]")
    print(f"    wanted  bbox [{', '.join(f'{v:.3f}' for v in want)}]  off by {off_by:.2f} deg")

    if head["numAddressedTiles"] == 0:
        msg = f"{args.slug}.pmtiles contains no tiles at all"
        print(("::warning::" if args.center_only else "::error::") + msg)
        return 0 if args.center_only else 1

    if not overlaps(have, want, slack=0.5):
        msg = (f"{args.slug}.pmtiles is an extract of the WRONG PLACE: its extent "
               f"does not overlap the city bbox at all ({off_by:.1f} deg away). "
               f"Every tile request over {args.slug} will come back empty. "
               f"Rebuild it with bbox {','.join(str(v) for v in want)}.")
        print(("::warning::" if args.center_only else "::error::") + msg)
        return 0 if args.center_only else 1

    if off_by > 0.75:
        print(f"::warning::{args.slug}.pmtiles overlaps the city but its centre is "
              f"{off_by:.2f} deg from the expected centre - check it is the right city")

    print(f"  {args.slug}.pmtiles OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
