// scripts/batch-split-pmtiles-buildings.mjs
// Batch runner that iterates over cities in cities-neighborhoods.json (or specified slugs),
// runs tile-join to strip/isolate buildings, and generates buildings.bin + buildings.json.
//
// Usage:
//   node scripts/batch-split-pmtiles-buildings.mjs --city amsterdam
//   node scripts/batch-split-pmtiles-buildings.mjs --city all --concurrency 4

import { execSync } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';

const argv = process.argv.slice(2);
const flag = (n, d) => {
  const i = argv.indexOf('--' + n);
  return i === -1 ? d : argv[i + 1];
};

const cityArg = flag('city', 'all');
const concurrency = parseInt(flag('concurrency', '4'), 10);

function getAllCitySlugs() {
  if (existsSync('cities-neighborhoods.json')) {
    try {
      const data = JSON.parse(readFileSync('cities-neighborhoods.json', 'utf8'));
      if (Array.isArray(data.cities)) {
        return data.cities.map(c => c.slug).filter(Boolean);
      }
    } catch (e) {
      console.warn('Failed to parse cities-neighborhoods.json:', e.message);
    }
  }

  if (existsSync('src/data/city-qid.json')) {
    try {
      const qids = JSON.parse(readFileSync('src/data/city-qid.json', 'utf8'));
      return Object.keys(qids).filter(k => !k.includes(' '));
    } catch {}
  }

  return [];
}

async function main() {
  let targetSlugs = [];
  if (cityArg === 'all') {
    targetSlugs = getAllCitySlugs();
  } else {
    targetSlugs = cityArg.split(',').map(s => s.trim().toLowerCase());
  }

  console.log(`[BATCH] Starting PMTiles building split for ${targetSlugs.length} cities (concurrency: ${concurrency})...`);

  let currentIndex = 0;
  async function worker(workerId) {
    while (currentIndex < targetSlugs.length) {
      const idx = currentIndex++;
      const slug = targetSlugs[idx];
      const progress = `[${idx + 1}/${targetSlugs.length}] [W${workerId}]`;
      try {
        console.log(`${progress} Processing ${slug}...`);
        execSync(`bash scripts/split-pmtiles-buildings.sh "${slug}"`, {
          stdio: 'inherit',
          env: { ...process.env }
        });
        console.log(`${progress} ✓ Completed ${slug}`);
      } catch (err) {
        console.error(`${progress} ✖ Failed ${slug}:`, err.message);
      }
    }
  }

  const workers = Array.from({ length: concurrency }, (_, i) => worker(i + 1));
  await Promise.all(workers);
  console.log(`[BATCH] All cities processed successfully.`);
}

main().catch(err => {
  console.error('[BATCH ERROR]', err);
  process.exit(1);
});
