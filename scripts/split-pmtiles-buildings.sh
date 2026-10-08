#!/usr/bin/env bash
# scripts/split-pmtiles-buildings.sh
# Strips the buildings layer from <slug>.pmtiles using tile-join,
# extracts building tiles, runs extract-pmtiles-buildings.mjs to generate
# buildings.bin and buildings.json, and uploads the artifacts back to R2.
#
# Usage:
#   ./scripts/split-pmtiles-buildings.sh amsterdam

set -uo pipefail

SLUG="${1:-}"
if [ -z "$SLUG" ]; then
  echo "Usage: $0 <city-slug>"
  exit 1
fi

R2_ACCOUNT_ID="${CF_ACCOUNT_ID:-${R2_DATALAKE_ACCOUNT_ID:-${R2_ACCOUNT_ID:-5d469620e5b9363beae1cb2e4e290aee}}}"
ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
WORKDIR="/tmp/pmtiles-${SLUG}"
mkdir -p "$WORKDIR"

echo "=== Processing city: ${SLUG} ==="

# 1. Download source PMTiles from R2
echo "[1/4] Downloading s3://globe/data/${SLUG}-2d/${SLUG}.pmtiles..."
if ! aws s3 cp "s3://globe/data/${SLUG}-2d/${SLUG}.pmtiles" "${WORKDIR}/input.pmtiles" --endpoint-url "$ENDPOINT"; then
  echo "  [SKIP] s3://globe/data/${SLUG}-2d/${SLUG}.pmtiles not found in R2."
  rm -rf "$WORKDIR"
  exit 0
fi

ORIGINAL_SIZE=$(stat -c%s "${WORKDIR}/input.pmtiles" 2>/dev/null || stat -f%z "${WORKDIR}/input.pmtiles" || echo 0)
echo "  Source PMTiles size: $((ORIGINAL_SIZE / 1024 / 1024)) MB"

# 2. Run tile-join: split into no-buildings PMTiles and building tile folder
echo "[2/4] Running tile-join to strip and isolate buildings..."
mkdir -p "${WORKDIR}/bldg_tiles"
tile-join -o "${WORKDIR}/${SLUG}-nobuildings.pmtiles" --exclude-layer=buildings --exclude-layer=building "${WORKDIR}/input.pmtiles" || true
tile-join -e "${WORKDIR}/bldg_tiles" --layer=buildings --layer=building "${WORKDIR}/input.pmtiles" || true

# 3. Convert building tiles to buildings.bin + buildings.json
echo "[3/4] Generating buildings.bin and buildings.json..."
mkdir -p "${WORKDIR}/out"
node scripts/extract-pmtiles-buildings.mjs \
  --tiles-dir "${WORKDIR}/bldg_tiles" \
  --out-dir "${WORKDIR}/out" \
  --city "${SLUG}" || true

# 4. Upload updated artifacts to R2 globe bucket
echo "[4/4] Uploading updated artifacts to R2 (s3://globe/data/${SLUG}-2d/)..."

if [ -f "${WORKDIR}/out/buildings.json" ] && [ -f "${WORKDIR}/out/buildings.bin" ]; then
  aws s3 cp "${WORKDIR}/out/buildings.json" "s3://globe/data/${SLUG}-2d/buildings.json" --endpoint-url "$ENDPOINT"
  aws s3 cp "${WORKDIR}/out/buildings.bin" "s3://globe/data/${SLUG}-2d/buildings.bin" --endpoint-url "$ENDPOINT"
  echo "  ✓ Uploaded buildings.json and buildings.bin"
else
  echo "  - No buildings extracted for ${SLUG}."
fi

if [ -f "${WORKDIR}/${SLUG}-nobuildings.pmtiles" ]; then
  STRIPPED_SIZE=$(stat -c%s "${WORKDIR}/${SLUG}-nobuildings.pmtiles" 2>/dev/null || stat -f%z "${WORKDIR}/${SLUG}-nobuildings.pmtiles" || echo 0)
  echo "  Stripped PMTiles size: $((STRIPPED_SIZE / 1024 / 1024)) MB (Saved: $(( (ORIGINAL_SIZE - STRIPPED_SIZE) / 1024 / 1024 )) MB)"
  aws s3 cp "${WORKDIR}/${SLUG}-nobuildings.pmtiles" "s3://globe/data/${SLUG}-2d/${SLUG}.pmtiles" --endpoint-url "$ENDPOINT"
  echo "  ✓ Uploaded stripped ${SLUG}.pmtiles"
fi

# Clean up temporary files
rm -rf "$WORKDIR"
echo "=== Done: ${SLUG} finished! ==="
