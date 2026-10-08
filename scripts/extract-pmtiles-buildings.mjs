// scripts/extract-pmtiles-buildings.mjs
// Extracts building footprints from an MVT vector tiles directory (exported by tile-join)
// or directly from a building PMTiles archive, and produces:
// 1. buildings.json (4-bit nibble spatial grid coverage)
// 2. buildings.bin (TKBL binary format)
//
// Usage:
//   node scripts/extract-pmtiles-buildings.mjs --tiles-dir ./building_tiles --out-dir ./out --city amsterdam
//   node scripts/extract-pmtiles-buildings.mjs --pmtiles ./amsterdam-buildings.pmtiles --out-dir ./out --city amsterdam

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { decompressSync } from 'fflate';

const require = createRequire(import.meta.url);
const { VectorTile } = require('@mapbox/vector-tile');
const PbfModule = require('pbf');
const PbfReader = PbfModule.PbfReader || PbfModule.default || PbfModule;

const argv = process.argv.slice(2);
const flag = (n, d) => {
  const i = argv.indexOf('--' + n);
  return i === -1 ? d : argv[i + 1];
};

const tilesDir = flag('tiles-dir', null);
const pmtilesPath = flag('pmtiles', null);
const outDir = flag('out-dir', './out');
const cityName = flag('city', 'city');
const targetZoom = parseInt(flag('zoom', '14'), 10);

if (!tilesDir && !pmtilesPath) {
  console.error('Error: specify either --tiles-dir <path> or --pmtiles <path>');
  process.exit(1);
}

fs.mkdirSync(outDir, { recursive: true });

// Standard grid parameters
const CELL_LON = 0.0009;   // ~100m longitude
const CELL_LAT = 0.000558; // ~100m latitude

/**
 * Tile pixel (0..extent) to WGS84 [lon, lat]
 */
function tileCoordsToLngLat(px, py, z, x, y, extent = 4096) {
  const n = Math.pow(2, z);
  const lon = ((x + px / extent) / n) * 360 - 180;
  const latRad = Math.atan(Math.sinh(Math.PI * (1 - 2 * (y + py / extent) / n)));
  const lat = (latRad * 180) / Math.PI;
  return [lon, lat];
}

/**
 * Find all .pbf / .mvt files recursively in a directory
 */
function findTileFiles(dir) {
  const results = [];
  function recurse(d) {
    for (const entry of fs.readdirSync(d, { withFileTypes: true })) {
      const full = path.join(d, entry.name);
      if (entry.isDirectory()) {
        recurse(full);
      } else if (entry.name.endsWith('.pbf') || entry.name.endsWith('.mvt')) {
        // Tile path format: .../<z>/<x>/<y>.pbf
        const parts = full.replace(/\\/g, '/').split('/');
        const len = parts.length;
        const z = parseInt(parts[len - 3], 10);
        const x = parseInt(parts[len - 2], 10);
        const y = parseInt(parts[len - 1].split('.')[0], 10);
        if (!isNaN(z) && !isNaN(x) && !isNaN(y)) {
          results.push({ path: full, z, x, y });
        }
      }
    }
  }
  recurse(dir);
  return results;
}

async function main() {
  console.log(`[EXTRACT] Processing buildings for ${cityName}...`);

  const buildingPolygons = []; // Array of { ring: Array<[lon, lat]>, center: [lon, lat] }
  let minLon = 180, maxLon = -180, minLat = 90, maxLat = -90;

  function processTileData(buf, z, x, y) {
    let pbfBuf = buf;
    try {
      pbfBuf = decompressSync(buf);
    } catch {}

    let vt;
    try {
      vt = new VectorTile(new PbfReader(pbfBuf));
    } catch {
      return;
    }

    const bldgLayer = vt.layers['buildings'] || vt.layers['building'];
    if (!bldgLayer) return;

    const extent = bldgLayer.extent || 4096;

    for (let i = 0; i < bldgLayer.length; i++) {
      const feat = bldgLayer.feature(i);
      // Type 3 = Polygon
      if (feat.type !== 3) continue;

      const geom = feat.loadGeometry();
      for (const ring of geom) {
        if (!ring || ring.length < 3) continue;

        let sumLon = 0, sumLat = 0;
        const coords = [];

        for (const pt of ring) {
          const [lon, lat] = tileCoordsToLngLat(pt.x, pt.y, z, x, y, extent);
          coords.push([lon, lat]);
          sumLon += lon;
          sumLat += lat;

          if (lon < minLon) minLon = lon;
          if (lon > maxLon) maxLon = lon;
          if (lat < minLat) minLat = lat;
          if (lat > maxLat) maxLat = lat;
        }

        const center = [sumLon / coords.length, sumLat / coords.length];
        buildingPolygons.push({ ring: coords, center });
      }
    }
  }

  if (tilesDir) {
    const tileFiles = findTileFiles(tilesDir);
    console.log(`  Found ${tileFiles.length} tile files in ${tilesDir}`);
    // Prefer highest available zoom (up to targetZoom)
    const maxZ = Math.max(...tileFiles.map(t => t.z));
    const activeTiles = tileFiles.filter(t => t.z === maxZ);
    console.log(`  Processing ${activeTiles.length} tiles at zoom z=${maxZ}...`);

    for (const t of activeTiles) {
      const buf = fs.readFileSync(t.path);
      processTileData(buf, t.z, t.x, t.y);
    }
  } else if (pmtilesPath) {
    const { PMTiles } = await import('pmtiles');
    const { FileSource } = await import('pmtiles/node');
    const p = new PMTiles(new FileSource(pmtilesPath));
    const header = await p.getHeader();
    console.log(`  Reading PMTiles minZoom=${header.minZoom}, maxZoom=${header.maxZoom}...`);

    const z = Math.min(targetZoom, header.maxZoom);
    // Read tiles at zoom level z
    for (let x = 0; x < Math.pow(2, z); x++) {
      for (let y = 0; y < Math.pow(2, z); y++) {
        const tile = await p.getZxy(z, x, y);
        if (tile && tile.data) {
          processTileData(new Uint8Array(tile.data), z, x, y);
        }
      }
    }
  }

  console.log(`  Extracted ${buildingPolygons.length} building rings.`);
  if (buildingPolygons.length === 0) {
    console.warn(`  Warning: No buildings found.`);
    return;
  }

  // --- 1. BUILD buildings.json (Spatial 4-bit nibble grid coverage) ---
  const w = Math.floor(minLon / CELL_LON) * CELL_LON;
  const s = Math.floor(minLat / CELL_LAT) * CELL_LAT;
  const nx = Math.ceil((maxLon - w) / CELL_LON) + 1;
  const ny = Math.ceil((maxLat - s) / CELL_LAT) + 1;

  console.log(`  Grid: [w=${w.toFixed(4)}, s=${s.toFixed(4)}] ${nx}x${ny} (${nx * ny} cells)`);

  const grid = new Uint8Array(nx * ny); // Density counts (0..15)
  for (const b of buildingPolygons) {
    const gx = Math.floor((b.center[0] - w) / CELL_LON);
    const gy = Math.floor((b.center[1] - s) / CELL_LAT);
    if (gx >= 0 && gx < nx && gy >= 0 && gy < ny) {
      const idx = gy * nx + gx;
      if (grid[idx] < 15) grid[idx]++;
    }
  }

  // Pack 2 cells per byte (4-bit nibbles: low = cell 2*i, high = cell 2*i + 1)
  const byteCount = Math.ceil((nx * ny) / 2);
  const covBuf = Buffer.alloc(byteCount);
  for (let i = 0; i < byteCount; i++) {
    const c1 = grid[i * 2] || 0;
    const c2 = grid[i * 2 + 1] || 0;
    covBuf[i] = (c1 & 0x0F) | ((c2 & 0x0F) << 4);
  }

  const buildingsJson = {
    v: 1,
    w: Number(w.toFixed(4)),
    s: Number(s.toFixed(4)),
    cellLon: CELL_LON,
    cellLat: CELL_LAT,
    nx,
    ny,
    cov: covBuf.toString('base64')
  };

  const jsonOutPath = path.join(outDir, 'buildings.json');
  fs.writeFileSync(jsonOutPath, JSON.stringify(buildingsJson));
  console.log(`  ✓ Wrote ${jsonOutPath} (${fs.statSync(jsonOutPath).size} bytes)`);

  // --- 2. BUILD buildings.bin (TKBL binary format) ---
  // Header: 32 bytes
  // - 0..3: ASCII 'TKBL'
  // - 4..7: version uint32 = 1
  // - 8..11: building count uint32
  // - 12..15: total vertices uint32
  // - 16..19: minLon * 1e6 int32
  // - 20..23: minLat * 1e6 int32
  // - 24..27: 0
  // - 28..31: 13
  let totalVerts = 0;
  for (const b of buildingPolygons) totalVerts += b.ring.length;

  const bldgCount = buildingPolygons.length;
  const headerSize = 32;
  const indexSize = bldgCount * 4;       // Uint32 offsets per building
  const vertSize = totalVerts * 4;        // 2 * Int16 per vertex (dx, dy relative to base)
  const totalBinSize = headerSize + indexSize + vertSize;

  const binBuf = Buffer.alloc(totalBinSize);
  binBuf.write('TKBL', 0, 4, 'ascii');
  binBuf.writeUInt32LE(1, 4);
  binBuf.writeUInt32LE(bldgCount, 8);
  binBuf.writeUInt32LE(totalVerts, 12);
  binBuf.writeInt32LE(Math.round(minLon * 1e6), 16);
  binBuf.writeInt32LE(Math.round(minLat * 1e6), 20);
  binBuf.writeInt32LE(0, 24);
  binBuf.writeInt32LE(13, 28);

  let currentVertOffset = 0;
  let vertByteOffset = headerSize + indexSize;

  const scale = 1e5; // Int16 precision offset from local origin

  for (let i = 0; i < bldgCount; i++) {
    binBuf.writeUInt32LE(currentVertOffset, headerSize + i * 4);
    const ring = buildingPolygons[i].ring;

    for (const [vLon, vLat] of ring) {
      const dx = Math.round((vLon - minLon) * scale);
      const dy = Math.round((vLat - minLat) * scale);
      binBuf.writeInt16LE(Math.max(-32768, Math.min(32767, dx)), vertByteOffset);
      binBuf.writeInt16LE(Math.max(-32768, Math.min(32767, dy)), vertByteOffset + 2);
      vertByteOffset += 4;
      currentVertOffset++;
    }
  }

  const binOutPath = path.join(outDir, 'buildings.bin');
  fs.writeFileSync(binOutPath, binBuf);
  console.log(`  ✓ Wrote ${binOutPath} (${(binBuf.length / (1024 * 1024)).toFixed(2)} MB)`);
}

main().catch(err => {
  console.error('[EXTRACT ERROR]', err);
  process.exit(1);
});
