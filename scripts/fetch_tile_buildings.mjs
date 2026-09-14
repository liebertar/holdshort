// Extracts the very buildings the screen draws (the building layer of OpenFreeMap vector tiles,
// render_height) as airspace data.
//
// Why the tiles: judging with NYC open data missed buildings that were in
// the tiles but not in the data, and a cleared corridor went straight through one. The buildings
// the judgement sees and the buildings the screen draws must be the same.
// Usage: node scripts/fetch_tile_buildings.mjs [min_height_m]
// Output: configs/airspace/nyc_buildings.json (same format as before). The local stack (3100)
// must be running.
import {createRequire} from 'node:module';
import {writeFileSync} from 'node:fs';
const require = createRequire(`${process.env.HOME}/.npm/_npx/6bcb61ec6d5aea22/node_modules/playwright/package.json`);
const {chromium} = require('playwright');

// Default 40 m: cruise is 90 m with 50 m roof clearance, so lower buildings never block the
// cruise leg. Extracting at 20 m yields 34,581 buildings (17 MB) and makes the planner several
// times slower.
const MIN_HEIGHT = Number(process.argv[2] || 20);   // lowest cruise 70 m − 50 m clearance: lower buildings keep 50 m at any cruise altitude
// Service area: a box holding 11 km around the depot (40.702, -73.970)
const BBOX = {south: 40.655, north: 40.805, west: -74.035, east: -73.895};
const STEP = {lat: 0.018, lon: 0.030};   // a little less than one 1400×900 screen covers at zoom 14

// The map screen (map.html) redraws every frame, so loaded() never settles. Use a separate
// blank map.
const PAGE = `<!doctype html><meta charset="utf-8"><div id="m" style="width:1400px;height:900px"></div>
<script src="https://cdn.jsdelivr.net/npm/maplibre-gl@5.9.0/dist/maplibre-gl.js"></script>
<script>
  const map = new maplibregl.Map({container:'m', style:'https://tiles.openfreemap.org/styles/positron',
    center:[-73.97, 40.72], zoom:14.2, pitch:0, attributionControl:false});
  window.__skynet = {map};
  map.on('load', () => { map.addLayer({id:'b', type:'fill', source:'openmaptiles', 'source-layer':'building',
    paint:{'fill-opacity':0.01}}); window.ready = true; });
</script>`;
const browser = await chromium.launch({channel: 'chrome', headless: true});
const page = await browser.newPage({viewport: {width: 1400, height: 900}});
await page.setContent(PAGE);
await page.waitForFunction(() => window.ready, null, {timeout: 60000});

const seen = new Map();
for (let lat = BBOX.south; lat < BBOX.north; lat += STEP.lat) {
  for (let lon = BBOX.west; lon < BBOX.east; lon += STEP.lon) {
    const rows = await page.evaluate(async ([lat, lon, minH]) => {
      const map = window.__skynet.map;
      map.jumpTo({center: [lon, lat], zoom: 14.2, pitch: 0, bearing: 0});
      await new Promise(resolve => map.once('idle', resolve));
      await new Promise(resolve => setTimeout(resolve, 200));
      const out = [];
      for (const f of map.querySourceFeatures('openmaptiles', {sourceLayer: 'building'})) {
        const h = Number(f.properties.render_height || 0);
        if (h < minH) continue;
        const polys = f.geometry.type === 'Polygon' ? [f.geometry.coordinates]
          : f.geometry.type === 'MultiPolygon' ? f.geometry.coordinates : [];
        for (const rings of polys) {
          const ring = rings[0].map(([x, y]) => [+y.toFixed(6), +x.toFixed(6)]);
          if (ring.length < 4) continue;
          out.push({ring, h: Math.round(h * 10) / 10, min_h: Number(f.properties.render_min_height || 0)});
        }
      }
      return out;
    }, [lat, lon, MIN_HEIGHT]);
    for (const r of rows) {
      // The same building arrives twice at tile edges. Keep one, keyed on first vertex + vertex
      // count + height.
      const key = `${r.ring[0][0]},${r.ring[0][1]},${r.ring.length},${r.h}`;
      if (!seen.has(key)) seen.set(key, r);
    }
  }
}
const volumes = [...seen.values()].map((r, i) => ({
  id: `bldg-t${String(i + 1).padStart(5, '0')}`,
  name: `BUILDING ${Math.round(r.h)} m`,
  polygon: r.ring.slice(0, -1),
  floor_m: 0.0,
  ceiling_m: r.h,
  reference: 'AGL',
  rule: 'forbidden',
  reason: `no flight through a building (roof ${Math.round(r.h)} m AGL)`,
  source: 'OpenFreeMap vector tiles (OpenStreetMap buildings, render_height)',
  tags: {render_height: r.h, render_min_height: r.min_h},
}));
writeFileSync('configs/airspace/nyc_buildings.json', JSON.stringify({
  source: 'OpenFreeMap / OpenStreetMap building layer, render_height — the same buildings the screen draws',
  fetched: new Date().toISOString(), min_height_m: MIN_HEIGHT, bbox: BBOX, volumes,
}));
console.log('buildings', volumes.length, 'tallest', Math.max(...volumes.map(v => v.ceiling_m)));
await browser.close();
