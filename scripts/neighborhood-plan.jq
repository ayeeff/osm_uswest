# Build the per-extract neighbourhood matrix.
#
# One jq invocation, no shell variables and no herestrings in between: the
# previous version chained four jq calls through <<<"$ALL" carrying ~93 KB of
# JSON, and a failure anywhere in that chain exited 1 with no message at all.
#
# Each output group is one geofabrik extract with its full city list, so the
# build job's matrix fans out per country file instead of per city. Each city is
# pre-joined to "slug:minLng,minLat,maxLng,maxLat" so the build job can read it
# out of an env var and split on the first colon; doing the join here avoids a
# second matrix dimension.
#
# Usage: jq -f neighborhood-plan.jq [--arg slug S] [--arg extract E] registry.json

# "slug:minLng,minLat,maxLng,maxLat" and, when the city has alternates,
# ":fallbackExtract1,fallbackExtract2". The build step splits on the first colon
# for the bbox, then on the first comma to separate any trailing fallback list.
# Keeping it one env var avoids a second matrix dimension.
def citypair:
  "\(.slug):\(.bbox | map(tostring) | join(","))"
  + (if (.fallback // []) | length > 0
     then ":" + (.fallback | join(","))
     else "" end);

# GitHub-hosted runners cap a job at 360 minutes, and each city costs one
# `osmium extract` plus two pyosmium passes. us-west is 328 cities in one
# country file, which does not fit, so each extract is chunked by $per. This
# also spreads the largest country across more runners instead of serialising it.
#
# Usage: jq -f neighborhood-plan.jq [--arg slug S] [--arg extract E] [--argjson per N] registry.json

# Only cities that name a geofabrik extract can be built here. The rest have no
# staged PBF and must not silently fall back to Overpass.
[ .cities[]
  | select(.extract != null and .extract != "")
  | select($slug == "" or .slug == $slug)
  | select($extract == "" or .extract == $extract)
]
| sort_by(.extract)
| group_by(.extract)
| map(
    . as $g
    # Group by geographic cluster within the extract. Clustering is what makes the
    # build step cheap: a job cuts the union bbox of its cities ONCE from the
    # region file, then cuts each city from that. `osmium extract -b` re-reads its
    # entire input for every bbox, so a job of 40 cities scattered across a 2-4 GiB
    # region costs 40 full scans. Measured at 34 min/city, which is ~23 hours for a
    # 40-city job, past the 360 min cap. Clusters cap the union at a few degrees, so
    # the per-city cuts read a small file instead.
    #
    # Cities with no cluster (an older registry) fall back to one cluster per
    # extract, which is the old behaviour, so a missing cluster degrades to slow
    # rather than to wrong.
    #
    # Built as one flat array per extract rather than a nested map(), because the
    # outer `add` only flattens one level and a nested map left arrays where the
    # build step expected objects: "Cannot index array with string cities".
    | [ .[] | { city: ., cluster: (.cluster // $g[0].extract) } ]
    | group_by(.cluster)
    | [
        .[] as $cl
        | [ $cl[].city | citypair ] as $all
        | (($all | length) / $per | ceil) as $n
        | range(0; $n) as $i
        | { extract: $g[0].extract, cities: $all[$i * $per : ($i + 1) * $per] }
      ]
  )
| add // []



