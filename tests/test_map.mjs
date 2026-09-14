// Run with: node --test tests/test_map.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as geometry from '../frontend/map-route.mjs';

const start = {lon:-73.97, lat:40.70};
const route = [{lon:-73.97, lat:40.71}, {lon:-73.96, lat:40.71},
  {lon:-73.96, lat:40.72}];

test('display curve preserves endpoints and leaves approved legs untouched', () => {
  const original = JSON.stringify(route);
  const curve = geometry.makeCurve(start, route);
  assert.deepEqual(curve.coordinates[0], [start.lon, start.lat]);
  assert.deepEqual(curve.coordinates.at(-1), [-73.96,40.72]);
  assert.equal(JSON.stringify(route), original);
  // The drawn line joins the judged legs straight; rounded corners would look like flying through a building.
  const legs = [start, ...route].map(p => [p.lon, p.lat]);
  const onALeg = ([x, y]) => legs.some((a, i) => i && (() => {
    const b = legs[i - 1], t = Math.hypot(a[0]-b[0], a[1]-b[1]);
    return Math.abs((x-b[0])*(a[1]-b[1]) - (y-b[1])*(a[0]-b[0])) / t < 1e-9;
  })());
  assert.ok(curve.coordinates.every(onALeg), 'a point bends outside the legs');
  assert.ok(curve.coordinates.flat().every(Number.isFinite));
});

test('motion crosses a waypoint along the rendered curve, not a diagonal shortcut', () => {
  const curve = geometry.makeCurve(start, route);
  const from = geometry.routeProgress(curve, {lon:-73.97,lat:40.708});
  const to = geometry.routeProgress(curve, {lon:-73.968,lat:40.71}, from);
  const m = {curve,from,to,at:0};
  assert.deepEqual(geometry.motionPoint(m,250,500), geometry.pointOnCurve(curve,(from+to)/2));
  assert.deepEqual(geometry.motionPoint(m,1000,500), geometry.pointOnCurve(curve,to));
  assert.ok(geometry.motionPoint(m,250,500)[1] > 40.7095);
});

test('remaining waypoints reuse a flight; replacements and altitude changes do not', () => {
  assert.equal(geometry.isRemainingRoute(route,route.slice(1)),true);
  assert.equal(geometry.isRemainingRoute(route,[]),true);
  assert.equal(geometry.isRemainingRoute(route,[{...route.at(-1),alt_m:60}]),false);
  assert.equal(geometry.isRemainingRoute(route,[start,...route]),false);
});

test('zero length and duplicate waypoints produce finite stationary positions', () => {
  const curve = geometry.makeCurve(start,[start,start]);
  assert.deepEqual(geometry.pointOnCurve(curve,0),[start.lon,start.lat]);
  assert.equal(geometry.routeProgress(curve,start),0);
});

// Exercise UI state transitions without a browser or network; this is not visual QA.
function scene(overrides = {}) {
  let now = 0;
  const elements = new Map(), sources = new Map(), layers = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id,
      {style:{},hidden:true,textContent:'',innerHTML:'',addEventListener(){}});
    return elements.get(id);
  };
  // Camera moves are recorded as calls only; this is where we read what one key did to the map.
  // The arguments are copied into this realm — an array made inside the vm has a different prototype, so
  // deepEqual fails even when the values are equal.
  const calls = [], record = name => (...args) => { calls.push([name, ...structuredClone(args)]); };
  const map = {on(){}, addControl(){}, keyboard:{disable:record('keyboard.disable')},
    panBy:record('panBy'), easeTo:record('easeTo'), flyTo:record('flyTo'),
    zoomIn:record('zoomIn'), zoomOut:record('zoomOut'), getBearing:()=>-28, getPitch:()=>45,
    addLayer(layer){layers.set(layer.id, structuredClone(layer));},
    setPaintProperty(id, name, value){layers.get(id).paint[name] = value;},
    getLayer(id){return layers.get(id);},
    getSource(id){
    if (!sources.has(id)) sources.set(id,{setData(data){this.data=data;}});
    return sources.get(id);
  }};
  const html = readFileSync(new URL('../frontend/map.html',import.meta.url),'utf8');
  const imports = Object.fromEntries(html.match(/import \{([^}]+)\}/)[1]
    .split(',').map(name=>[name.trim(), geometry[name.trim()]]));
  const {document:documentOverrides, ...rest} = overrides;
  const context = vm.createContext({...imports, console, Date, Map, Set, Math,
    location:{hostname:'localhost'}, performance:{now:()=>now},
    requestAnimationFrame(){}, document:{getElementById:element,querySelector:element, ...documentOverrides},
    maplibregl:{Map:function(){return map;}, NavigationControl:function(){}, AttributionControl:function(){},
      Popup:function(){return {setLngLat(){return this;}, setHTML(){return this;},
        addTo(){return this;}};}}, ...rest});
  const code = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
    .replace(/^import .*?;\n/m,'');
  vm.runInContext(code,context);
  const run = (name,...args) => context[name](...args);
  return {run,get:name=>context[name],element,source:id=>sources.get(id)?.data,
    path:phase=>(sources.get('flightpath')?.data?.features || [])
      .filter(f=>f.properties.phase === phase),
    layer:id=>layers.get(id), time:value=>{now=value;}, calls, html};
}

function endOf(path) {
  const ring = path.at(-1).geometry.coordinates[0];
  return ring[1].map((v, i) => (v + ring[2][i]) / 2);
}

// The default is on the ground (alt 0). The negotiation replay only runs for a ground start, so pass alt_m
// to see an aircraft already in the air.
function snapshot(tick=1, round=1, remaining=route, position=start) {
  const world = {assets:{'drone-01':{id:'drone-01',battery:80,alt_m:0,...position,
      route:remaining.map(p => ({...p, alt_m:110}))}},
    depot_coords:start,scoreboard:{spend_usd:0},fleet_limit:450};
  return {tick,round,recall_tick:null,worlds:{guarded:world,direct:structuredClone(world)}};
}
function denial(id='denied-1', extra={}) {
  return {id,at:Date.now()/1000,outcome:'denied',
    proposal:{asset_id:'drone-01',action:'fly_route',params:{legs:[start,...route]}},
    decision:{verdict:'denied',reason:'no-fly zone <test>'},...extra};
}

function approval(id='approved-1', extra={}) {
  return {id,at:Date.now()/1000,outcome:'executed',
    proposal:{asset_id:'drone-01',action:'fly_route',params:{legs:[start,...route]}},
    decision:{verdict:'auto',reason:'within limits'},...extra};
}

test('a partial draw keeps the curve endpoints and stays inside the route', () => {
  const curve = geometry.makeCurve(start,route);
  const total = curve.progress.at(-1);
  const half = geometry.sliceCurve(curve,0,total/2);
  assert.deepEqual(half[0],[start.lon,start.lat]);
  assert.ok(half.length < geometry.sliceCurve(curve,0,total).length);
  assert.deepEqual(geometry.sliceCurve(curve,0,total).at(-1),curve.coordinates.at(-1));
  assert.ok(half.flat().every(Number.isFinite));
});

const {GROW_MS, CHECK_MS, HOLD_MS, FADE_MS, APPROVED_HOLD_MS} = geometry;

test('a route is drawn, waits for a verdict, then holds and fades where it is', () => {
  assert.deepEqual(geometry.stageWindow('approved',0),[0,0]);
  assert.deepEqual(geometry.stageWindow('approved',GROW_MS),[0,1]);
  assert.equal(geometry.stageWindow('approved',geometry.stageLife('approved')),null);
  // A refused line stays fully drawn, then fades. It never rewinds.
  assert.deepEqual(geometry.stageWindow('rejected',GROW_MS + CHECK_MS + HOLD_MS),[0,1]);
  assert.equal(geometry.stageWindow('rejected',geometry.stageLife('rejected')),null);
  assert.equal(geometry.stageFade('rejected',GROW_MS + CHECK_MS + HOLD_MS - 10),1);
  const fading = geometry.stageFade('rejected',GROW_MS + CHECK_MS + HOLD_MS + FADE_MS / 2);
  assert.ok(fading > 0 && fading < 1);
  assert.equal(geometry.stageFade('rejected',geometry.stageLife('rejected')),0);
  // There has to be a beat after the drawing where it waits for the verdict, or you cannot see what was decided.
  assert.equal(geometry.stagePhase('rejected', GROW_MS / 2),'drawing');
  assert.equal(geometry.stagePhase('rejected', GROW_MS + 10),'checking');
  assert.equal(geometry.stagePhase('rejected', GROW_MS + CHECK_MS + 10),'refused');
  assert.equal(geometry.stagePhase('approved', GROW_MS + CHECK_MS + 10),'approved');
});

test('the label sits at the start of the route, not on the moving head', () => {
  const curve = geometry.makeCurve(start, route);
  assert.deepEqual(geometry.labelAnchor(curve), curve.coordinates[0]);
  assert.deepEqual(geometry.labelAnchor(curve), [start.lon, start.lat]);
});

test('the flight path floats at the approved altitude, segment by segment', () => {
  const climb = [{lon:-73.97,lat:40.71,alt_m:60},{lon:-73.96,lat:40.71,alt_m:120}];
  const pieces = geometry.ribbon([{lon:-73.97,lat:40.70,alt_m:60},...climb]);
  assert.equal(pieces.length,2);
  assert.ok(pieces[0].base > 0, 'stuck to the ground it cannot show altitude');
  assert.ok(pieces[1].base > pieces[0].base, 'legs approved at different altitudes need plates at different heights');
  assert.ok(pieces.every(p => p.height > p.base && p.polygon.length === 5));
  assert.ok(pieces.flatMap(p => p.polygon).flat().every(Number.isFinite));
});

test('a snapshot raises a flight path for a drone that is flying', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(),null);
  ui.run('draw');
  const path = ui.source('flightpath').features;
  assert.ok(path.length, 'an approved route should raise a flight path');
  assert.ok(path.every(f => f.properties.height > f.properties.base));
  ui.run('renderSnapshot',snapshot(1,2,[]),null); ui.run('draw');
  assert.equal(ui.source('flightpath').features.length, 0);
});

test('a drone that has stopped to work says so next to its name', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(1,1,[],{lon:-73.97,lat:40.70,state:'dropping',load:2}),null);
  ui.run('draw');
  // It writes the number of boxes left — the same count as the boxes coming off one by one.
  assert.equal(ui.source('guarded').features[0].properties.work,'unloading 2');
  ui.run('renderSnapshot',snapshot(1,1,[],{lon:-73.97,lat:40.70,state:'loading',load:3}),null);
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.work,'loading 3/6');
  assert.equal(ui.source('cargo').features.length, 3, 'one box is stacked per box loaded');
  ui.run('renderSnapshot',snapshot(2,1,route,{lon:-73.97,lat:40.70,state:'delivering'}),null);
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.work,'',
               'nothing is attached while flying');
});

test('warehouse, curved green path and drone update from snapshots and clear on reset', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(),null);
  ui.run('draw');
  assert.equal(ui.source('depot').features[0].geometry.coordinates[0],start.lon);
  const first = ui.path('approved');
  ui.time(500);
  ui.run('renderSnapshot',snapshot(2,1,route.slice(1),{lon:-73.969,lat:40.71}),null);
  ui.time(750); ui.run('draw');
  const later = ui.path('approved');
  // The curve is not rebuilt (the same curve even as waypoints drop off); only the flown part is erased.
  assert.deepEqual(later.at(-1).geometry, first.at(-1).geometry, 'the destination must not move');
  assert.ok(later.length < first.length, 'the flown part is not being erased');
  assert.ok(ui.source('guarded').features[0].geometry.coordinates[1] > 40.70);
  ui.run('renderSnapshot',snapshot(1,2,[]),null); ui.run('draw');
  assert.equal(ui.path('approved').length,0);
});

test('final denials alert once, grow the submitted legs, and expire without polling', () => {
  const ui = scene(), e = denial();
  ui.run('renderDenials',{ledger:[{...e,outcome:'pending'}]},0);
  assert.equal(ui.element('denial').hidden,true);
  ui.run('renderDenials',{ledger:[e]},100);
  assert.equal(ui.element('denial').hidden,false);
  assert.equal(ui.element('denial-who').textContent, 'drone-01');
  assert.equal(ui.element('denial-what').textContent, 'delivery route');
  assert.ok(ui.element('denial-why').textContent.includes('<test>'));
  // A refused route grows out from the drone, and only once fully grown reaches the end of the filed legs.
  ui.time(200); ui.run('draw');
  assert.equal(ui.path('rejected').length, 0, 'nothing is red before the verdict');
  const partial = ui.path('pending');
  ui.time(100 + GROW_MS + CHECK_MS + 50); ui.run('draw');
  const full = ui.path('rejected');
  assert.match(ui.source('stage-label').features[0].properties.label,/^REJECTED/);
  assert.ok(full.length > partial.length);
  assert.ok(endOf(full)[1] <= route.at(-1).lat);
  ui.run('renderDenials',{ledger:[e]},7000);
  ui.time(100 + geometry.stageLife('rejected') + 50); ui.run('draw');
  assert.equal(ui.path('rejected').length,0);
  ui.time(8101); ui.run('draw');   // the alert lasts 8 s
  assert.equal(ui.element('denial').hidden,true);
  assert.equal(ui.path('rejected').length,0);
});

test('an approved corridor grows yellow before changing colour and carrying the flight', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(),null);
  ui.run('renderDenials',{ledger:[approval()]},0);
  ui.time(100); ui.run('draw');
  const growing = ui.path('pending');
  assert.ok(growing.length > 0);           // before the verdict, so not green yet
  assert.equal(ui.path('approved').length,0);
  assert.equal(ui.source('stage-label').features[0].properties.label,'PLANNING…');
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.match(ui.source('stage-label').features[0].properties.label,/^APPROVED · /);
  ui.time(geometry.stageLife('approved') + 50); ui.run('draw');
  const settled = ui.path('approved');
  assert.ok(growing.length < settled.length);
  assert.ok(endOf(settled)[1] <= route.at(-1).lat);
});

test('the ledger is newest first, so stages are re-sorted into the order they happened', () => {
  const ui = scene();
  const at = Date.now() / 1000;
  const legs = [start, ...route];
  const rejected = {id:'r', at: at - 0.2, outcome:'denied',
    proposal:{asset_id:'drone-01', action:'fly_route', params:{legs}},
    decision:{verdict:'denied', reason:'x'}};
  const approved = {id:'a', at, outcome:'executed',
    proposal:{asset_id:'drone-01', action:'fly_route', params:{legs}},
    decision:{verdict:'auto', reason:'ok'}};
  ui.run('renderDenials', {ledger:[approved, rejected]}, 0);   // newest first
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  // The refusal happened first. If the approval replays first, the order was flipped.
  assert.ok(ui.path('rejected').length > 0);
  assert.equal(ui.path('approved').length, 0);
});

test('a route refused for a held pad says so, and draws no blocker', () => {
  const ui = scene();
  const e = denial('held',{decision:{verdict:'denied',reason:'x',code:'resource_held',
    detail:{resource:'pad:launch',holder:'drone-03'}}});
  ui.run('renderDenials',{ledger:[e]},0);
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  const label = ui.source('stage-label').features[0].properties.label;
  assert.match(label,/^REJECTED · pad:launch is held by drone-03/);
  assert.equal(ui.source('blocker').features.length,0);
  assert.equal(ui.source('breach').features.length,0);
});

test('provenance: tier tag from the model id, drafter word, and the approved headline names the rule', () => {
  const ui = scene();
  const llm = {enabled:true, host:'ollama', models:{nano:'nemotron-3-nano', super:'', ultra:''}};
  const wrote = approval('w1', {proposal:{asset_id:'drone-01', action:'fly_route', author:'nemotron-3-nano',
    params:{legs:[start,...route], drafter:'nano:nemotron-3-nano'}}, decision:{verdict:'auto', reason:'within limits', code:'within_limits'}});
  const byRules = approval('w2', {proposal:{asset_id:'drone-02', action:'fly_route', author:'rules',
    params:{legs:[start,...route], drafter:'astar'}}, decision:{verdict:'auto', reason:'within limits', code:'within_limits'}});
  ui.run('renderSnapshot', snapshot(), {ledger:[wrote, byRules], llm, locks:{}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /drone-01<\/b> <span class="tier">nano<\/span>/, 'a form the model wrote gets a one-word tier');
  assert.ok(!/nemotron-3-nano/.test(feed), 'the full model id never reaches the screen');
  assert.ok(!/drone-02<\/b> <span class="tier">/.test(feed), 'what the rules wrote carries no tag');
  assert.match(feed, /delivery route · A\*/);
  assert.match(ui.element('llm-line').textContent, /^runtime · rules · drones · Nemotron Nano$/);
  ui.time(100); ui.run('draw');
  const labels = ui.source('stage-label').features.map(f => f.properties.label);
  assert.ok(labels.some(l => l === 'PLANNING… · nano'), labels.join('|'));
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  const later = ui.source('stage-label').features.map(f => f.properties.label);
  assert.ok(later.some(l => /^APPROVED · within limits$/.test(l)), later.join('|'));
  ui.run('renderSnapshot', snapshot(), {ledger:[], llm:{enabled:false, models:{}}, locks:{}});
  assert.match(ui.element('llm-line').textContent, /rules only/);
});

test('a traffic refusal names the other aircraft and blinks its corridor; a delayed approval says who it waited for', () => {
  const ui = scene();
  const crossing = denial('x1', {proposal:{asset_id:'drone-01', action:'fly_route',
    params:{legs:[start,...route], blocked_kind:'traffic', blocked_asset:'drone-03', blocked_leg:1,
            blocked_at:{lat:route[0].lat, lon:route[0].lon}}},
    decision:{verdict:'denied', reason:'it crosses a building', code:'airspace'}});
  ui.run('renderDenials',{ledger:[crossing]},0);
  ui.run('corridorAlpha','drone-03',1);
  ui.time(GROW_MS + CHECK_MS + HOLD_MS / 6); ui.run('draw');
  assert.match(ui.source('stage-label').features[0].properties.label, /^REJECTED · CROSSES drone-03/);
  assert.equal(ui.source('blocker').features.length, 0, 'a crossing is an aircraft, not a polygon');
  assert.ok(ui.layer('flightpath:drone-03').paint['fill-extrusion-opacity'] < .85, 'the other corridor blinks');
  const later = approval('x2', {proposal:{asset_id:'drone-02', action:'fly_route',
    params:{legs:[start,...route], resolution:'delay', holding_for:'drone-03'}},
    decision:{verdict:'auto', reason:'within limits', code:'within_limits'}});
  ui.run('renderDenials',{ledger:[later]},0);
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  const labels = ui.source('stage-label').features.map(f => f.properties.label);
  assert.ok(labels.some(l => /^APPROVED · after drone-03$/.test(l)), labels.join('|'));
  ui.run('renderSnapshot', snapshot(1,1,[],{...start, state:'ready', holding_for:'drone-03'}), null);
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.work, 'holding for drone-03');
});

test('a route approved while already in the air is not replayed from the old spot', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(1,1,[],{...start,alt_m:55}),null);
  ui.run('renderDenials',{ledger:[approval()]},0);
  ui.time(GROW_MS / 2); ui.run('draw');
  assert.equal(ui.path('pending').length,0,'no yellow line is redrawn from the old spot while in the air');
  assert.equal(ui.source('stage-label').features.length,0);
});

test('a queued decision has not travelled anywhere yet, so nothing is drawn', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[approval('q',{decision:{verdict:'queued',reason:'waiting for another filing'}})]},0);
  ui.time(100); ui.run('draw');
  assert.equal(ui.path('rejected').length,0);
  assert.equal(ui.path('approved').length,0);
});

test('old ledger entries do not replay alerts; non-flight denials do not invent paths', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[denial('old',{at:Date.now()/1000-60})]},0);
  assert.equal(ui.element('denial').hidden,true);
  ui.run('renderDenials',{ledger:[denial('charge',{
    proposal:{asset_id:'drone-01',action:'fast_charge',params:{}},
  })]},100);
  assert.equal(ui.element('denial').hidden,false);
  ui.time(1050); ui.run('draw');
  assert.equal(ui.path('rejected').length,0);
  assert.equal(ui.element('denial-what').textContent,'fast charge');
});

test('route completion clears approval after animation and round reset clears alerts', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(),null);
  ui.time(500);
  ui.run('renderSnapshot',snapshot(2,1,[],route.at(-1)),{ledger:[denial()]});
  ui.time(750); ui.run('draw');
  assert.equal(ui.path('approved').length,0, 'no flight corridor overlaps a refusal replay');
  ui.time(1001); ui.run('draw');
  assert.equal(ui.path('approved').length,0);
  ui.run('renderSnapshot',snapshot(1,2,[]),null); ui.run('draw');
  assert.equal(ui.element('denial').hidden,true);
  assert.equal(ui.path('rejected').length,0);
});


test('one elevated geometry changes colour, blinks independently and fades without ground sources', () => {
  const ui = scene();
  const legs = [start,...route].map(p=>({...p,alt_m:55}));
  const e = denial('colour',{proposal:{asset_id:'drone-01',action:'fly_route',params:{legs}}});
  ui.run('renderDenials',{ledger:[e]},0);
  ui.time(GROW_MS + CHECK_MS / 2); ui.run('draw');
  const yellow = ui.path('pending').map(f=>f.geometry);
  assert.ok(yellow.length);
  assert.ok(ui.path('pending').every(f=>f.properties.base > 0));
  ui.time(GROW_MS + CHECK_MS); ui.run('draw');
  assert.deepEqual(ui.path('rejected').map(f=>f.geometry),yellow);
  const bright = ui.layer('flightpath:drone-01').paint['fill-extrusion-opacity'];
  ui.run('corridorAlpha','drone-02',1);
  ui.time(GROW_MS + CHECK_MS + HOLD_MS / 6); ui.run('draw');
  assert.ok(ui.layer('flightpath:drone-01').paint['fill-extrusion-opacity'] < bright);
  assert.equal(ui.layer('flightpath:drone-02').paint['fill-extrusion-opacity'],bright);
  ui.time(geometry.stageLife('rejected') - 10); ui.run('draw');
  assert.ok(ui.layer('flightpath:drone-01').paint['fill-extrusion-opacity'] < .1);
  for (const id of ['pending','approved','rejected']) assert.equal(ui.source(id),undefined);
});

test('the elevated curve keeps per-leg altitude and fixed dash positions after flight progress', () => {
  const c = geometry.makeCurve({...start,alt_m:55},route.map((p,i)=>({...p,alt_m:55+i*10})));
  const all = geometry.curveRibbon(c,0,c.progress.at(-1));
  const remaining = geometry.curveRibbon(c,c.progress.at(-1)/2,c.progress.at(-1));
  assert.deepEqual(remaining.at(-1),all.at(-1));
  assert.ok(remaining.length < all.length);
  // The top of the ribbon sits 4.5 m below the aircraft altitude (RIBBON_DROP_M), and it is 3 m thick.
  assert.equal(all[0].height,50.5);
  assert.equal(all[0].base,47.5);
  assert.equal(all.at(-1).height,70.5);
});

test('a vertex where the altitude changes carries a vertical dotted column shaped like the corridor dashes', () => {
  const level = geometry.makeCurve({...start, alt_m:0}, [{...route[0], alt_m:90}, {...route[1], alt_m:90}]);
  const stepped = geometry.makeCurve({...start, alt_m:0}, [{...route[0], alt_m:60}, {...route[1], alt_m:100}]);
  const total = stepped.lengths.at(-1);
  assert.ok(geometry.curveRibbon(stepped, 0, total).every(p => !p.column), 'corridor pieces carry corridor only');
  const columns = geometry.curveColumns(stepped, 0, total);
  assert.ok(columns.length > 0 && columns.every(p => p.column));
  const [lon, lat] = stepped.points[1];
  const atVertex = columns.filter(p => Math.abs(p.polygon[0][0] - lon) < 2e-4 && Math.abs(p.polygon[0][1] - lat) < 2e-4);
  const takeoff = columns.filter(p => Math.abs(p.polygon[0][1] - start.lat) < 2e-4);
  assert.ok(atVertex.length >= 1, `vertex column pieces ${atVertex.length}`);
  assert.ok(takeoff.length >= 1, `takeoff column pieces ${takeoff.length}`);
  // The column runs from the top of the 60 m plate (55.5) to the top of the 100 m plate (95.5); each piece
  // is at most 36 m, the same as the corridor dashes.
  assert.ok(atVertex.every(p => p.base >= 55.4 && p.height <= 95.6 && p.height - p.base <= 36.01));
  // The plate footprint is the corridor width (18 m) by its thickness (3 m). Not a cube.
  const metres = (a, b) => Math.hypot((a[0] - b[0]) * Math.cos(lat * Math.PI / 180), a[1] - b[1]) * 111320;
  const ring = atVertex[0].polygon;
  const sides = [metres(ring[0], ring[1]), metres(ring[1], ring[2])].sort((x, y) => y - x);
  assert.ok(Math.abs(sides[0] - 18) < 0.5 && Math.abs(sides[1] - 3) < 0.5, `footprint ${sides.map(v => v.toFixed(1))}`);
  assert.equal(geometry.curveColumns(level, 0, level.lengths.at(-1)).filter(p => Math.abs(p.polygon[0][1] - start.lat) > 2e-4).length, 0,
    'no vertex column when the altitude does not change');
  assert.equal(geometry.curveColumns(stepped, total * 0.9, total).length, 0,
    'a column at a vertex already flown is outside the window');
});

test('a duplicate refusal is neither replayed as a route nor raised as a card', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[denial('dup',{decision:{verdict:'denied',reason:'the same filing was executed a moment ago',code:'duplicate'}})]},0);
  assert.equal(ui.element('denial').hidden, true);
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.equal(ui.path('rejected').length, 0);
});

test('an endpoint refusal names the gap, not a leg; a withdrawal is its own card', () => {
  const ui = scene();
  const gap = denial('e1', {proposal:{asset_id:'drone-01', action:'fly_route',
    params:{legs:[start,...route], blocked_kind:'origin', blocked_leg:0, blocked_gap_m:240.4,
            blocked_at:{lat:start.lat, lon:start.lon}}},
    decision:{verdict:'denied', reason:'the start point is inside a forbidden zone', code:'airspace'}});
  ui.run('renderDenials',{ledger:[gap]},0);
  assert.equal(ui.element('denial-why').textContent, 'START 240 m FROM THE AIRCRAFT');
  assert.equal(ui.element('denial-more').textContent, '', 'nothing blocked it, so no altitude band either');
  ui.time(GROW_MS + CHECK_MS + HOLD_MS / 6); ui.run('draw');
  assert.match(ui.source('stage-label').features[0].properties.label, /^REJECTED · START 240 m FROM THE AIRCRAFT/);
  const withdrawn = {id:'w1', at:Date.now()/1000, outcome:'done',
    proposal:{asset_id:'drone-03', action:'divert_ground', author:'runtime',
              params:{withdrawn_for:'drone-02', intent:'i1'}},
    decision:{verdict:'auto', reason:'overlaps the airborne re-filing of drone-02', policy_hit:'traffic', code:'withdrawn', detail:{for:'drone-02'}}};
  ui.run('renderDenials',{ledger:[withdrawn]},0);
  assert.equal(ui.element('#denial .tag').textContent, 'WITHDRAWN');
  assert.match(ui.element('denial-why').textContent, /drone-02/);
  assert.doesNotMatch(ui.element('denial-why').textContent, /a rule arrived/);
});

test('the banner says who read a NOTAM, and shows the raw text while nobody has', () => {
  const ui = scene();
  const bulletin = {id:'n1', kind:'notam', text:'AREA BOUNDED BY 404310N0735920W SFC-400FT AGL 0907-0912Z',
                    published_tick:525, until_tick:900};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[]});
  assert.match(ui.element('banner').innerHTML, /NOTAM/);
  assert.match(ui.element('banner').innerHTML, /not yet read/);
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[
    {id:'n1', name:'East Village helipad', applied:true, held:false, source:'grammar', from_tick:525, until_tick:900}]});
  assert.match(ui.element('banner').innerHTML, /East Village helipad/);
  assert.match(ui.element('banner').innerHTML, /rule grammar/);
  assert.doesNotMatch(ui.element('banner').innerHTML, /not yet read/);
  ui.run('renderSnapshot', {...snapshot(), bulletins:[]}, {ledger:[], notices:[
    {id:'n2', name:'Harlem TFR', applied:false, held:true, source:'model:nvidia/nemotron-3-super-120b-a12b'}]});
  assert.match(ui.element('banner').innerHTML, /<i>waiting for a person<\/i>/, 'waiting for a person is italic');
  assert.equal(ui.element('banner').hidden, false);
});

test('a notice a person confirmed before its window says so, and is neither raw nor enforced', () => {
  const ui = scene();
  const bulletin = {id:'n3', kind:'notam', text:'MEDEVAC INBOUND HARLEM', published_tick:1350, until_tick:2100};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[
    {id:'n3', name:'Harlem TFR', applied:false, held:false, source:'human', from_tick:1350, until_tick:2100,
     polygon:[[40.81, -73.94], [40.81, -73.93], [40.82, -73.93]]}]});
  const banner = ui.element('banner').innerHTML;
  assert.match(banner, /confirmed by a person/);
  assert.match(banner, /applies when the window opens/);
  assert.doesNotMatch(banner, /not yet read/);
  assert.doesNotMatch(banner, /pulled back/);
  assert.equal(ui.source('zone').features.length, 0, 'confirmed but not yet applied is not painted');
});

test('a ledger line that went to a person is not replayed as an approved corridor', () => {
  const ui = scene();
  ui.run('renderSnapshot', snapshot(), {ledger:[{id:'h1', at:Date.now()/1000, outcome:'waiting',
    proposal:{asset_id:'drone-01', action:'fly_route', params:{legs:[start, ...route]}},
    decision:{verdict:'human', reason:'over the aircraft limit: $490 > $320', code:'over_asset', detail:{spent:490, cap:320}}}]});
  assert.equal(ui.path('approved').length, 0);
  assert.equal(ui.path('pending').length, 0);
  assert.match(ui.element('feed').innerHTML, /HUMAN/);
  assert.match(ui.element('feed').innerHTML, /aircraft cap: \$490 > \$320/);
});

// Runtime advisory, in exactly the shape the runtime puts on /state.advisories.
function advisory(extra={}) {
  return {asset:'drone-02', tick:640, at:Date.now()/1000, ledger_id:'l_adv1', trigger:'refusals',
    refusals:[{tick:600, code:'airspace', blocked_kind:'traffic', blocked_asset:'drone-03', blocked_until_tick:700},
              {tick:610, code:'airspace', blocked_kind:'traffic', blocked_asset:'drone-03', blocked_until_tick:700},
              {tick:620, code:'airspace', blocked_kind:'forbidden', blocked_volume:'bldg-1'}],
    options:[{id:'hold', label:'hold on the ground until tick 700', legal:true, why:'drone-03 clears that volume at tick 700', until_tick:700},
             {id:'climb', label:'climb +30 m on the last filed legs', legal:false, why:'bldg-1 crossed 10 m above the roof', shift_m:30},
             {id:'decline', label:'decline the job', legal:true, why:'no aircraft flies'},
             {id:'escalate', label:'escalate to a person', legal:true, why:'a controller looks'}],
    chosen:'hold', summary:'', model:'', source:'rules', ...extra};
}

test('a runtime advisory card names the aircraft, lists the checked options, and hides after 12 s', () => {
  const ui = scene();
  ui.run('renderAdvisories', {advisories:[advisory()]}, 100);
  assert.equal(ui.element('advisory').hidden, false);
  assert.equal(ui.element('advisory-tag').textContent, 'RUNTIME ADVISORY');
  assert.equal(ui.element('advisory-who').textContent, 'drone-02');
  assert.equal(ui.element('advisory-source').textContent, 'rules · after 3 refusals in a row');
  // An advisory the rules chose is assembled from screen words — the runtime's own sentence is not reused.
  assert.equal(ui.element('advisory-summary').textContent,
    'drone-02 was refused 3 times in a row. The rules suggest: hold on the ground until tick 700.');
  const options = ui.element('advisory-options').innerHTML;
  assert.match(options, /<li class="ok chosen">hold on the ground until tick 700 · legal<\/li>/);
  assert.match(options, /<li class="no">climb \+30 m on the filed legs · not legal — bldg-1 crossed 10 m above the roof<\/li>/);
  assert.match(options, /<li class="ok">decline the order · legal<\/li>/);
  assert.match(options, /<li class="ok">escalate to a person · legal<\/li>/);
  assert.match(ui.element('advisory-note').textContent, /information only/);
  // The same advisory is not raised twice, and it goes down after 12 s.
  ui.time(100 + 12001); ui.run('draw');
  assert.equal(ui.element('advisory').hidden, true);
  ui.run('renderAdvisories', {advisories:[advisory()]}, 13000);
  assert.equal(ui.element('advisory').hidden, true);
  // A summary the model (super) wrote shows as it is, and the source word changes.
  ui.run('renderAdvisories', {advisories:[advisory({ledger_id:'l_adv2', source:'super', chosen:'decline',
    model:'nemotron-3-super', summary:'Two filings crossed drone-03 and the third hit a roof. Decline this order and refile after tick 700.'})]}, 14000);
  assert.equal(ui.element('advisory').hidden, false);
  assert.match(ui.element('advisory-source').textContent, /^super · after 3 refusals in a row$/);
  assert.match(ui.element('advisory-summary').textContent, /^Two filings crossed drone-03/);
  assert.match(ui.element('advisory-options').innerHTML, /<li class="ok chosen">decline the order · legal<\/li>/);
  // An advisory after a decline names that as its trigger.
  ui.run('renderAdvisories', {advisories:[advisory({ledger_id:'l_adv3', trigger:'decline_after_refusals', chosen:'escalate'})]}, 15000);
  assert.equal(ui.element('advisory-source').textContent, 'rules · declined the order after 3 refusals');
  assert.match(ui.element('advisory-summary').textContent, /declined the order after 3 refusals\. The rules suggest: escalate to a person\./);
});

test('the feed shows the advisory as a runtime line naming the chosen option; it raises no denial card', () => {
  const ui = scene();
  const entry = {id:'l_adv1', at:Date.now()/1000, outcome:'noted',
    proposal:{asset_id:'drone-02', action:'advisory', author:'runtime',
              params:{options:advisory().options, chosen:'hold', trigger:'refusals'}},
    decision:{verdict:'auto', reason:'…', code:'advisory',
              detail:{resource:'drone-02', chosen:'hold', trigger:'refusals', source:'rules'}}};
  ui.run('renderSnapshot', snapshot(), {ledger:[entry], llm:{enabled:false, models:{}}, locks:{}, advisories:[]});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /<b>drone-02<\/b> runtime advisory/);
  assert.match(feed, /advisory · hold on the ground until tick 700/);
  assert.equal(ui.element('denial').hidden, true, 'an advisory is not a refusal card');
  ui.time(100); ui.run('draw');
  assert.equal(ui.path('pending').length, 0, 'an advisory is not a route, so nothing is replayed');
  const bySuper = {...entry, id:'l_adv2', decision:{...entry.decision, detail:{...entry.decision.detail, source:'super'}}};
  ui.run('renderSnapshot', snapshot(), {ledger:[bySuper], llm:{enabled:false, models:{}}, locks:{}, advisories:[]});
  assert.match(ui.element('feed').innerHTML, /hold on the ground until tick 700 · super/);
});

test('the api ports come from ?rt= and ?sim=, and default to 8000/8100', () => {
  // A module const is not a global of the vm context, so read it off the debug handle (window.__skynet.api).
  const api = ui => JSON.stringify(ui.get('window').__skynet.api);
  const plain = scene({window:{}});
  assert.equal(api(plain), JSON.stringify({sim:'http://localhost:8100', rt:'http://localhost:8000'}));
  const second = scene({window:{}, location:{hostname:'localhost', search:'?rt=8010&sim=8110'}, URLSearchParams});
  assert.equal(api(second), JSON.stringify({sim:'http://localhost:8110', rt:'http://localhost:8010'}));
});

test('an applied runtime notice is painted on the ground; a held one is not', () => {
  const ui = scene();
  const ring = [[40.81, -73.94], [40.81, -73.93], [40.82, -73.93]];
  const held = {id:'n2', name:'Harlem', applied:false, held:true, polygon:ring, source:'model:x'};
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[held]});
  assert.equal(ui.source('zone').features.length, 0, 'a held notice blocks nothing');
  assert.match(ui.element('banner').innerHTML, /waiting for a person/);
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[{...held, applied:true, held:false, source:'human'}]});
  assert.equal(ui.source('zone').features.length, 1);
  assert.equal(ui.source('zone').features[0].properties.id, 'n2');
  assert.equal(ui.source('zone').features[0].geometry.coordinates[0].length, 4, 'the ring is closed');
  assert.match(ui.element('banner').innerHTML, /read by a person/);
});

test('notice ledger lines read as words, not codes', () => {
  const ui = scene();
  const line = (id, code, detail={}) => ({id, at:Date.now()/1000, outcome:'unreadable',
    proposal:{asset_id:'airspace', action:'publish_notice', author:'runtime', params:{notice_id:'n9'}},
    decision:{verdict:'denied', reason:'…', code, detail}});
  ui.run('renderSnapshot', snapshot(), {ledger:[line('u1', 'notice_unreadable', {notice:'n9', why:'no model'}),
    line('p1', 'notice_published'), line('r1', 'notice_refused')], llm:{enabled:false, models:{}}, locks:{}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /runtime<\/b> airspace notice/);
  assert.match(feed, /not read by the runtime \(no model\)/);
  assert.match(feed, /confirmed by a person/);
  assert.match(feed, /refused by a person/);
  assert.doesNotMatch(feed, /r_notice/);
});

test('an approval that lands while the refusal is still playing is replayed yellow after it, not dropped', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[denial('r1')]},0);
  ui.time(1000); ui.run('draw');                       // the red is still drawing
  ui.run('renderDenials',{ledger:[approval('a1'), denial('r1')]},1000);
  ui.time(1016); ui.run('draw');                       // next frame: the queued approval must survive
  const red = GROW_MS + CHECK_MS + HOLD_MS + FADE_MS;
  ui.time(red + 400); ui.run('draw');                  // just after the red ends: the yellow is growing
  assert.ok(ui.path('pending').length > 0, 'the approval replay starts yellow');
  assert.equal(ui.path('approved').length, 0, 'green is not yet');
  ui.time(red + GROW_MS + CHECK_MS + 200); ui.run('draw');
  assert.ok(ui.path('approved').length > 0, 'green comes after that');
});

test('a backlog of refusals is collapsed so the replay never falls more than one stage behind', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[denial('b1')]},0);
  ui.time(500); ui.run('draw');
  const more = ['b2','b3','b4','b5'].map(id => denial(id));
  ui.run('renderDenials',{ledger:[...more.reverse(), denial('b1')]},500);
  ui.run('renderDenials',{ledger:[approval('ok'), ...more, denial('b1')]},600);
  const red = GROW_MS + CHECK_MS + HOLD_MS + FADE_MS;
  ui.time(red + 300); ui.run('draw');
  assert.ok(ui.path('pending').length > 0,
    'the approval comes straight after the first red; the four backed-up refusals are dropped');
});

// Information intake. Weather is a policy (takeoffs stopped), an incident is a notice (a zone) — in exactly
// the shape of weather · incidents · intake on /state.
test('the banner says a weather hold and who read it, a held report waits for a person, an unread bulletin shows raw', () => {
  const ui = scene();
  const bulletin = {id:'wx-1', kind:'weather', text:'KNYC 0929Z WIND 240 AT 18 GUST 28 KT VIS 2SM RA',
                    published_tick:2175, until_tick:2700};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[], intake:{items:[]}, weather:{hold:null, held:[]}});
  assert.match(ui.element('banner').innerHTML, /WEATHER.*not yet read/);
  const hold = {id:'wx-1', reason:'WEATHER HOLD · gusts 14 m/s > 12', until_tick:2700, since_tick:2200, source:'grammar', report:{}};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[],
    intake:{items:[{id:'wx-1', kind:'weather', read_by:'grammar'}]}, weather:{hold, held:[]}});
  const banner = ui.element('banner').innerHTML;
  assert.match(banner, /<b>WEATHER HOLD<\/b> · gusts 14 m\/s &gt; 12 · takeoffs held until tick 2700 — read by the rule grammar/);
  assert.doesNotMatch(banner, /not yet read/);
  assert.equal(ui.element('banner').hidden, false);
  // A report the model read waits for a person — it stops nothing and says so.
  ui.run('renderSnapshot', {...snapshot(), bulletins:[]}, {ledger:[], notices:[], intake:{items:[]},
    weather:{hold:null, held:[{id:'wx-2', breaches:['gusts 20 m/s > 12'], source:'model:nvidia/nemotron-3-super-120b-a12b'}]},
    llm:{enabled:true, models:{super:'nvidia/nemotron-3-super-120b-a12b'}}});
  assert.match(ui.element('banner').innerHTML, /gusts 20 m\/s &gt; 12 — read by the super agent, <i>waiting for a person<\/i>/);
  // What could not be read shows raw, with the reason.
  ui.run('renderSnapshot', {...snapshot(), bulletins:[]}, {ledger:[], notices:[],
    intake:{items:[{id:'t1', kind:null, why:'no model', text:'Gusty afternoon <b>expected</b>'}]}, weather:{hold:null, held:[]}});
  assert.match(ui.element('banner').innerHTML, /INTAKE<\/b> · Gusty afternoon &lt;b&gt;expected&lt;\/b&gt; — not read by the runtime \(no model\)/);
});

test('an incident is painted like a zone and named on the banner; held it is neither', () => {
  const ui = scene();
  const ring = [[40.705, -74.015], [40.705, -74.012], [40.703, -74.012], [40.703, -74.015]];
  const notice = {id:'fdny-1', name:'FIRE · 1 Bowling Green', kind:'incident', applied:true, held:false,
                  source:'grammar', until_tick:3600, polygon:ring};
  const incident = {id:'fdny-1', name:'FIRE · 1 Bowling Green', kind:'fire', radius_m:200, until_tick:3600, applied:true, held:false};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[{id:'fdny-1', kind:'incident', text:'FDNY 3-ALARM FIRE AT 1 BOWLING GREEN', published_tick:3000, until_tick:3600}]},
    {ledger:[], notices:[notice], incidents:[incident], intake:{items:[{id:'fdny-1', kind:'incident', read_by:'grammar'}]}, weather:{hold:null, held:[]}});
  const banner = ui.element('banner').innerHTML;
  assert.match(banner, /<b>FIRE · 1 Bowling Green<\/b> · 200 m keep-out until tick 3600 — read by the rule grammar/);
  assert.match(banner, /landing areas inside unusable/);
  assert.doesNotMatch(banner, /not yet read/);
  assert.equal(ui.source('zone').features.length, 1);
  assert.equal(ui.source('zone').features[0].properties.id, 'fdny-1');
  assert.equal(ui.source('zone').features[0].properties.name, 'FIRE · 1 Bowling Green',
               'the circle needs its name, so you can see what is closed even when buildings hide it');
  const held = {...notice, applied:false, held:true, source:'model:x'};
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[held], incidents:[{...incident, applied:false, held:true}], intake:{items:[]}, weather:{hold:null, held:[]}});
  assert.equal(ui.source('zone').features.length, 0, 'a held incident blocks nothing');
  assert.match(ui.element('banner').innerHTML, /FIRE · 1 Bowling Green<\/b> · 200 m keep-out — read by the Agent agent, <i>waiting for a person<\/i>/);
});

test('a takeoff refused by the weather hold says WEATHER HOLD; a landing refused by the incident names it', () => {
  const ui = scene();
  const held = denial('wx-deny', {decision:{verdict:'denied', reason:'WEATHER HOLD · gusts 14 m/s > 12 (weather-hold:fly_route)',
    code:'policy', policy_hit:'weather-hold:fly_route', detail:{policy:'weather-hold:fly_route', until_tick:2700}}});
  ui.run('renderDenials', {ledger:[held]}, 0);
  assert.equal(ui.element('denial-why').textContent, 'WEATHER HOLD · takeoffs held until tick 2700');
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.match(ui.source('stage-label').features[0].properties.label, /^REJECTED · WEATHER HOLD · takeoffs held until tick 2700/);
  assert.equal(ui.source('blocker').features.length, 0, 'a hold is not a polygon');
  // A landing area inside the incident circle. The runtime hands over the blocking zone's name as blocked_name.
  const fire = denial('fire-deny', {proposal:{asset_id:'drone-02', action:'fly_route',
    params:{legs:[start, ...route], blocked_kind:'landing', blocked_volume:'fdny-1', blocked_name:'FIRE · 1 Bowling Green',
            blocked_leg:3, blocked_at:{lat:route.at(-1).lat, lon:route.at(-1).lon},
            blocked_polygon:[[40.705, -74.015], [40.705, -74.012], [40.703, -74.012]], blocked_floor_m:0, blocked_ceiling_m:null}},
    decision:{verdict:'denied', reason:'the landing area is ringed by FIRE · 1 Bowling Green', code:'airspace', policy_hit:'airspace', forbids:'fdny-1'}});
  ui.run('renderDenials', {ledger:[fire]}, 0);
  assert.equal(ui.element('denial-why').textContent, 'NO ROOM TO LAND · FIRE · 1 Bowling Green');
  const depart = denial('dep-deny', {proposal:{asset_id:'drone-03', action:'depart', params:{}},
    decision:{verdict:'denied', reason:'WEATHER HOLD · gusts 14 m/s > 12 (weather-hold:depart)', code:'policy',
              policy_hit:'weather-hold:depart', detail:{policy:'weather-hold:depart', until_tick:2700}}});
  ui.run('renderDenials', {ledger:[depart]}, 100);
  assert.equal(ui.element('denial-what').textContent, 'depart');
  assert.match(ui.element('denial-why').textContent, /^WEATHER HOLD/);
});

test('intake and weather ledger lines read as words, and opening a hold raises no recall card', () => {
  const ui = scene();
  const line = (id, action, code, detail={}, extra={}) => ({id, at:Date.now()/1000, outcome:'noted',
    proposal:{asset_id:'intake', action, author:'runtime', params:{}, ...extra},
    decision:{verdict:'auto', reason:'WEATHER HOLD · gusts 14 m/s > 12', code, detail}});
  const entries = [
    line('i1', 'intake', 'intake_received', {source:'sim'}),
    line('i2', 'intake', 'intake_read', {kind:'weather', read_by:'grammar'}),
    line('i3', 'intake', 'intake_unreadable', {why:'no model'}),
    line('w1', 'weather_hold', 'weather_hold', {until_tick:2700}, {asset_id:'fleet'}),
    line('w2', 'weather_hold', 'weather_hold_expired', {until_tick:2700}, {asset_id:'fleet'}),
    line('k1', 'incident_keepout', 'incident_keepout', {name:'FIRE · 1 Bowling Green', radius_m:200, until_tick:3600}, {asset_id:'fleet'}),
    {...line('l1', 'lift_weather_hold', 'weather_hold_lifted', {}, {asset_id:'fleet'}), outcome:'done',
     decision:{verdict:'auto', reason:'x', code:'weather_hold_lifted', approved_by:'controller'}},
  ];
  ui.run('renderSnapshot', snapshot(), {ledger:entries, llm:{enabled:false, models:{}}, locks:{}, notices:[], intake:{items:[]}, weather:{hold:null, held:[]}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /runtime<\/b> information intake[\s\S]*received from sim/);
  assert.match(feed, /read by the rule grammar · weather/);
  assert.match(feed, /not read \(no model\)/);
  assert.match(feed, /runtime<\/b> weather hold[\s\S]*WEATHER HOLD · gusts 14 m\/s > 12 · takeoffs held until tick 2700/);
  assert.match(feed, /weather hold expired at tick 2700/);
  assert.match(feed, /incident keep-out[\s\S]*FIRE · 1 Bowling Green · 200 m keep-out until tick 3600/);
  assert.match(feed, /lift the weather hold[\s\S]*weather hold lifted by a person · controller/);
  assert.doesNotMatch(feed, /r_intake|r_weather/);
  ui.run('renderDenials', {ledger:entries}, 0);
  assert.equal(ui.element('denial').hidden, true, 'opening a hold is not a recall card');
});

// The line under the aircraft name: which model flies this aircraft — the runtime's /state.agents says so.
test('the second label line names the model flying the aircraft, or "rules" when there is none', () => {
  const ui = scene();
  ui.run('renderSnapshot', snapshot(), {ledger:[], llm:{enabled:true, models:{nano:'nemotron-3-nano:4b'}, host:'ollama'},
    agents:{'drone-01':{model:'nemotron-3-nano:4b', host:'ollama', world:'guarded', last_seen_tick:1, display:'Nemotron Nano 4B'}}});
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.sub, 'Nemotron Nano 4B · ↑ 110 m');
  // The model line on the header card also counts the aircraft agents.
  assert.equal(ui.element('llm-line').textContent, 'runtime · rules · drones · Nemotron Nano 4B ×1');
  // No field (an older runtime, or a rules fleet) means rules. With no altitude, just the model name.
  const grounded = snapshot(1, 1, [], {lon:-73.97, lat:40.70, state:'ready'});
  ui.run('renderSnapshot', grounded, {ledger:[], llm:{enabled:false, models:{}}});
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.sub, 'rules');
  assert.doesNotMatch(ui.element('llm-line').textContent, /drones/);
  // With agents present but all on rules, "rules only" is not followed by the same words again.
  ui.run('renderSnapshot', grounded, {ledger:[], llm:{enabled:false, models:{}},
    agents:{'drone-01':{model:'', host:'', world:'guarded', last_seen_tick:1, display:''}}});
  assert.equal(ui.element('llm-line').textContent, 'rules only — no agent model configured');
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.sub, 'rules');
  // An empty display falls back to a tidied model id; with neither it is rules.
  assert.equal(ui.run('modelName', 'nemotron-3-nano:4b'), 'Nemotron Nano 4B');
  assert.equal(ui.run('modelName', 'gpt-oss-20b'), 'Gpt Oss 20B');
  assert.equal(ui.run('modelName', ''), '');
});

// An empty card is an empty box. Before there is content there is no card; it appears with its first content.
test('cards with nothing to say are hidden; they appear with their first content', () => {
  const ui = scene();
  for (const id of ['acts', 'legend', 'banner', 'lostlink', 'denial', 'advisory'])
    assert.equal(ui.element(id).hidden, true, `${id} should start hidden`);
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[]});
  assert.equal(ui.element('legend').hidden, false);
  assert.equal(ui.element('acts').hidden, true, 'no ledger, no ledger card');
  assert.equal(ui.element('banner').hidden, true);
  assert.equal(ui.element('lostlink').hidden, true);
  assert.equal(ui.element('round-tick').textContent, 'round 1 · tick 1');
  ui.run('renderSnapshot', snapshot(), {ledger:[approval()], notices:[]});
  assert.equal(ui.element('acts').hidden, false);
  assert.equal(ui.element('feed').innerHTML.match(/class="ev/g).length, 1);
  // The legend shows only what is on screen. No grid means no ceiling colour and no source line.
  const legend = ui.element('legend').innerHTML;
  assert.match(legend, /APPROVED/);
  assert.match(legend, /Warehouse/);
  assert.doesNotMatch(legend, /No-fly grid|Ceiling|FAA UAS/);
  assert.doesNotMatch(legend, /Closed zone|DRONE LANDING AREA/);
  const snap = snapshot();
  snap.worlds.guarded.bands = [{name:'b', rule:'ceiling', ceiling_m:61, polygon:[[40.7, -73.98], [40.7, -73.97], [40.71, -73.97]]}];
  snap.worlds.guarded.landing_areas = [{name:'Pier 17', lat:40.706, lon:-74.002}];
  snap.worlds.guarded.zone = {active:true, id:'z1', name:'Z', polygon:[[40.7, -73.98], [40.7, -73.97], [40.71, -73.97]]};
  ui.run('renderSnapshot', snap, {ledger:[], notices:[]});
  const full = ui.element('legend').innerHTML;
  assert.match(full, /No-fly grid[\s\S]*Ceiling[\s\S]*122&nbsp;m/);
  assert.match(full, /FAA UAS Facility Map/);
  assert.match(full, /DRONE LANDING AREA/);
  assert.match(full, /Closed zone/);
});

test('the feed keeps ten lines at most', () => {
  const ui = scene();
  const many = Array.from({length:14}, (_, i) => approval(`a${i}`));
  ui.run('renderSnapshot', snapshot(), {ledger:many, llm:{enabled:false, models:{}}});
  assert.equal(ui.element('feed').innerHTML.match(/class="ev/g).length, 10);
});

// Keys. What the card lists is all of them, and the map does not have to be clicked first.
test('the keys card is three plain lines, and the keys it lists move the map', () => {
  const handlers = {};
  const ui = scene({document:{addEventListener(type, fn){ handlers[type] = fn; }}});
  const keys = ui.html.match(/<div class="card meta" id="keys"[\s\S]*?<\/div>\n<\/div>/)[0];
  assert.doesNotMatch(keys, /<kbd/, 'the key shapes are drawings — not kbd chips');
  assert.deepEqual([...keys.matchAll(/data-t="(keys_\w+)"/g)].map(m => m[1]),
    ['keys_title', 'keys_mouse', 'keys_arrows', 'keys_letters'], 'one spoken sentence per line');
  // A tall rectangle: one control per row (three mouse, six keyboard).
  const grid = keys.match(/<div class="kgrid" aria-hidden="true">([\s\S]*?)\n  <\/div>/)[1];
  assert.equal((grid.match(/class="kk"/g) || []).length, 10, 'ten control rows');
  assert.equal((grid.match(/<svg class="mouse"/g) || []).length, 3, 'mouse left · wheel · right');
  assert.equal((grid.match(/class="kc"/g) || []).length, 11, 'Ctrl · arrows · Shift+arrows · + − · N · H · 1–4 · L');
  assert.ok(!/<button|tabindex/.test(grid), 'the key shapes on the card cannot be pressed');
  assert.doesNotMatch(keys, /⇧/, 'letters instead of Mac symbols');
  assert.match(grid, /class="kc">Shift</);
  assert.match(grid, /class="kc">Ctrl</, 'Ctrl+drag for trackpads');
  const t = ui.get('t');
  assert.equal(t('keys_mouse'), 'drag pan · scroll zoom · right-drag or Ctrl+drag orbit');
  assert.equal(t('keys_arrows'), 'arrows pan · Shift+arrows rotate/tilt · +/- zoom');
  assert.equal(t('keys_letters'), 'N north · H home · 1-4 focus drone · L runtime links');
  // MapLibre's own key handling is disabled — with both alive one press would move the map twice.
  assert.ok(ui.calls.some(c => c[0] === 'keyboard.disable'));
  ui.run('renderSnapshot', snapshot(), null);
  ui.calls.length = 0;
  const press = (key, extra = {}) => {
    let prevented = false;
    handlers.keydown({key, target:{tagName:'BODY'}, preventDefault(){ prevented = true; }, ...extra});
    return prevented;
  };
  assert.equal(press('ArrowRight'), true);
  assert.deepEqual(ui.calls.at(-1).slice(0, 2), ['panBy', [100, 0]], 'the right arrow pans right');
  press('ArrowUp');
  assert.deepEqual(ui.calls.at(-1)[1], [0, -100]);
  press('ArrowLeft', {shiftKey:true});
  assert.equal(ui.calls.at(-1)[0], 'easeTo');
  assert.equal(ui.calls.at(-1)[1].bearing, -58, 'Shift+left rotates');
  press('ArrowUp', {shiftKey:true});
  assert.equal(ui.calls.at(-1)[1].pitch, 60, 'Shift+up tilts');
  press('+'); assert.equal(ui.calls.at(-1)[0], 'zoomIn');
  press('-'); assert.equal(ui.calls.at(-1)[0], 'zoomOut');
  press('n'); assert.equal(ui.calls.at(-1)[1].bearing, 0);
  press('h'); assert.equal(ui.calls.at(-1)[0], 'flyTo');
  press('1'); assert.equal(ui.calls.at(-1)[0], 'flyTo');
  assert.equal(ui.calls.at(-1)[1].zoom, 16.4, 'a number key flies to that aircraft');
  // L toggles the runtime links. It is not a camera key, so it must not move the map.
  const cameraCalls = ui.calls.length;
  assert.equal(press('l'), true);
  assert.equal(ui.calls.length, cameraCalls, 'L does not move the camera');
  assert.equal(ui.source('runtime').features.length, 0, 'switching them off removes the mast too');
  press('l');
  // While typing, or with a modifier held, the keys are not the map's.
  const before = ui.calls.length;
  assert.equal(press('ArrowRight', {target:{tagName:'INPUT'}}), false);
  assert.equal(press('ArrowRight', {metaKey:true}), false);
  assert.equal(press('x'), false);
  assert.equal(ui.calls.length, before);
});

// Lost link. That the runtime still holds that aircraft's space shows together on the label, the corridor,
// the card, the ledger and the legend.
test('a lost link is named on the aircraft, keeps its corridor with a pulsing shell, and raises a card until restored', () => {
  const ui = scene();
  const lost = {links:{'drone-01':{status:'lost', since_tick:400, last_seen_tick:399}}, ledger:[], notices:[]};
  ui.run('renderSnapshot', snapshot(2, 1, route, {lon:-73.97, lat:40.705, alt_m:110, state:'delivering'}), lost);
  ui.time(700); ui.run('draw');
  const label = ui.source('guarded').features[0].properties;
  assert.equal(label.work, 'LOST LINK', 'lost link in the state slot');
  assert.equal(label.tone, '#ff9d95');
  assert.ok(ui.path('approved').length > 0, 'the corridor stays — the space is still reserved');
  const shell = ui.path('shell');
  assert.ok(shell.length > 0, 'a shell around the corridor');
  assert.ok(shell.every(f => f.properties.height > f.properties.base));
  const opacity = ui.layer('flightpath-shell').paint['fill-extrusion-opacity'];
  assert.ok(opacity >= .25 && opacity <= .85, `the outline is inside the breathing range ${opacity}`);
  ui.time(1500); ui.run('draw');
  assert.notEqual(ui.layer('flightpath-shell').paint['fill-extrusion-opacity'], opacity, 'the shell breathes');
  const card = ui.element('lostlink');
  assert.equal(card.hidden, false);
  assert.match(card.innerHTML, /LOST LINK<\/span><b>drone-01<\/b><span class="meta">since tick 400 · space reserved/);
  assert.equal(card.dataset?.asset ?? 'drone-01', 'drone-01');
  assert.match(ui.element('legend').innerHTML, /RESERVED \(lost link\)/);
  // Once it is restored, all of it goes away.
  const back = {links:{'drone-01':{status:'ok', since_tick:420, last_seen_tick:420}}, ledger:[], notices:[]};
  ui.run('renderSnapshot', snapshot(3, 1, route, {lon:-73.97, lat:40.706, alt_m:110, state:'delivering'}), back);
  ui.time(2200); ui.run('draw');
  assert.equal(ui.element('lostlink').hidden, true);
  assert.equal(ui.path('shell').length, 0);
  assert.equal(ui.layer('flightpath-shell').paint['fill-extrusion-opacity'], 0);
  assert.equal(ui.source('guarded').features[0].properties.work, '');
  assert.doesNotMatch(ui.element('legend').innerHTML, /RESERVED/);
  // When the runtime does not carry links yet (an older runtime), nobody is cut off.
  ui.run('renderSnapshot', snapshot(4, 1, route, {lon:-73.97, lat:40.707, alt_m:110, state:'delivering'}), {ledger:[]});
  ui.run('draw');
  assert.equal(ui.element('lostlink').hidden, true);
});

test('link ledger lines read as words and raise no denial card', () => {
  const ui = scene();
  const line = (id, code, detail) => ({id, at:Date.now()/1000, outcome:'noted',
    proposal:{asset_id:'drone-02', action:code, author:'runtime', params:{}},
    decision:{verdict:'auto', reason:'…', code, detail}});
  ui.run('renderSnapshot', snapshot(), {ledger:[line('l1', 'link_lost', {since_tick:400, last_seen_tick:399}),
    line('l2', 'link_restored', {since_tick:400, restored_tick:431})], llm:{enabled:false, models:{}}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /drone-02<\/b> link lost[\s\S]*no telemetry since tick 400 — the filed space stays reserved/);
  assert.match(feed, /drone-02<\/b> link restored[\s\S]*telemetry back — reserved space released/);
  assert.doesNotMatch(feed, /r_link/);
  assert.equal(ui.element('denial').hidden, true);
});

// The outline is four thin rails at the corners. On a single due-north leg the sideways offset is purely a
// longitude difference, so it can be measured in metres.
test('the corridor outline is four thin rails just outside the corridor, at its altitude', () => {
  const north = geometry.makeCurve({...start, alt_m:55}, [{lon:start.lon, lat:40.71, alt_m:55}]);
  const total = north.progress.at(-1);
  const inner = geometry.curveRibbon(north, 0, total);
  const shell = geometry.curveShell(north, 0, total);
  assert.ok(shell.length > 0 && shell.flatMap(p => p.polygon).flat().every(Number.isFinite));
  const east = lon => (lon - start.lon) * Math.cos(start.lat * Math.PI / 180) * 110570;
  const offsets = shell.map(p => p.polygon.slice(0, 4).reduce((sum, [lon]) => sum + east(lon), 0) / 4);
  const widths = shell.map(p => { const xs = p.polygon.map(([lon]) => east(lon)); return Math.max(...xs) - Math.min(...xs); });
  assert.ok(offsets.every(x => Math.abs(Math.abs(x) - 14) < 0.3),
    `rails sit 14 m from the centre (corridor half-width 9 + 5): ${offsets.map(x => x.toFixed(1))}`);
  assert.ok(offsets.some(x => x > 0) && offsets.some(x => x < 0), 'on both sides');
  assert.ok(widths.every(w => Math.abs(w - 1.6) < 0.1), `rails stay thin: ${widths.map(w => w.toFixed(2))}`);
  assert.ok(shell.every(p => Math.abs(p.height - p.base - 1.6) < 1e-9), 'rails are 1.6 m thick too');
  // They wrap the corridor by 5 m above and below — the top rail's top face and the bottom rail's underside.
  const corridor = {base:Math.min(...inner.map(p => p.base)), height:Math.max(...inner.map(p => p.height))};
  assert.ok(Math.abs(Math.max(...shell.map(p => p.height)) - (corridor.height + 5)) < 1e-9);
  assert.ok(Math.abs(Math.min(...shell.map(p => p.base)) - (corridor.base - 5)) < 1e-9);
  // With no sideways offset, ribbon is unchanged (an 18 m corridor plate, on the centre line).
  const plate = geometry.ribbon([{lon:start.lon, lat:40.70, alt_m:55}, {lon:start.lon, lat:40.71, alt_m:55}])[0];
  const xs = plate.polygon.map(([lon]) => east(lon));
  assert.ok(Math.abs(Math.max(...xs) - 9) < 0.1 && Math.abs(Math.min(...xs) + 9) < 0.1);
});

// The approval screen (approvals.html). A lost-link notice card comes up in plain words, and the banner
// names the aircraft that was cut off.
async function approvals(state, compare) {
  const html = readFileSync(new URL('../frontend/approvals.html', import.meta.url), 'utf8');
  const elements = new Map();
  const noop = new Proxy({}, {get: () => () => {}});
  const element = id => {
    if (!elements.has(id)) elements.set(id, {style:{}, innerHTML:'', textContent:'', clientWidth:100, clientHeight:60,
      getContext: () => noop, dataset:{}});
    return elements.get(id);
  };
  const context = vm.createContext({console, Date, Map, Set, Math, Number, Object, JSON, String,
    location:{hostname:'localhost'}, window:{devicePixelRatio:1},
    document:{getElementById:element, addEventListener(){}},
    setInterval(){}, fetch: url => Promise.resolve({json: () => Promise.resolve(url.endsWith('/state') ? state : compare)})});
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  await context.refresh();
  return {element, html};
}

test('the approval screen lists a lost-link notice as a human decision and names the aircraft on its banner', async () => {
  const world = {assets:{}, pads:{}, scoreboard:{spend_usd:0, human_approvals:0}, fleet_limit:450, events:[]};
  const compare = {tick:431, recall_tick:null, worlds:{guarded:world, direct:structuredClone(world)}};
  const state = {llm:{enabled:false, models:{}}, ledger:[], incidents:[], weather:{},
    links:{'drone-02':{status:'lost', since_tick:400, last_seen_tick:399}, 'drone-01':{status:'ok', since_tick:0, last_seen_tick:431}},
    awaiting_human:[{id:'p1', asset_id:'drone-02', action:'lost_link_notice', cost_usd:0, blast_radius:'schedule',
                     rationale:'no telemetry since tick 400 <b>'}]};
  const ui = await approvals(state, compare);
  const inbox = ui.element('inbox').innerHTML;
  assert.match(inbox, /<b>drone-02<\/b> · lost-link notice/);
  assert.match(inbox, /no telemetry since tick 400 &lt;b&gt;/, 'model and runtime sentences never run as markup');
  assert.match(inbox, /data-ok="p1"/);
  const banner = ui.element('recall').innerHTML;
  assert.match(banner, /drone-02 lost its link \(since tick 400\)/);
  assert.doesNotMatch(banner, /drone-01/);
  assert.equal(ui.element('recall').style.display, 'block');
  // Nobody cut off and no cards means no banner either.
  const quiet = await approvals({...state, links:{}, awaiting_human:[]}, compare);
  assert.equal(quiet.element('recall').style.display, 'none');
  assert.match(quiet.element('inbox').innerHTML, /<div class="empty">none<\/div>/);
});

// Locally the runtime stand-in (the Super slot) and the aircraft model share one 4B id. Tagging a drone
// request "super" would read as the drone calling a 120B — the aircraft line names the model carried on that
// aircraft, and the runtime line is separate.
test('a drone request is tagged with the drone\'s own model even when the runtime stand-in shares its id', () => {
  const ui = scene();
  const runtime = {ledger:[approval('m1', {proposal:{asset_id:'drone-01', action:'fly_route',
      author:'nemotron-3-nano:4b', params:{legs:[start,...route], drafter:'astar'}},
      decision:{verdict:'auto', reason:'within limits', code:'within_limits'}})],
    llm:{enabled:true, host:'ollama', models:{nano:'', super:'nemotron-3-nano:4b', ultra:''}},
    agents:{'drone-01':{model:'nemotron-3-nano:4b', host:'ollama', world:'guarded', last_seen_tick:1,
      display:'Nemotron Nano 4B'}}, locks:{}};
  ui.run('renderSnapshot', snapshot(), runtime);
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /drone-01<\/b> <span class="tier">Nano 4B<\/span>/, feed);
  assert.doesNotMatch(feed, /class="tier">super</);
  assert.equal(ui.element('llm-line').textContent,
    'runtime · Nemotron Nano 4B (Super stand-in) · ollama · drones · Nemotron Nano 4B ×1');
  // With a real Super on the runtime there is no stand-in tag.
  ui.run('renderSnapshot', snapshot(), {...runtime,
    llm:{enabled:true, host:'nebius', models:{nano:'', super:'nvidia/nemotron-3-super-120b-a12b', ultra:''}}});
  assert.match(ui.element('llm-line').textContent, /^runtime · Nemotron Super 120B · nebius · drones/);
});

// Every panel minimizes and expands. The state persists in storage, and the screen still works when storage
// is blocked.
test('every panel minimizes and expands, the state persists, and a blocked storage breaks nothing', () => {
  const saved = {};
  const ui = scene({localStorage:{getItem:key => key === 'skynet-panels' ? '{"keys":true}' : null,
                                  setItem(key, value){ saved[key] = value; }}});
  assert.equal(ui.run('isMinimized', 'keys'), true, 'it starts from the stored state');
  ui.run('applyLanguage');
  assert.equal(ui.element('keys').min, true);
  for (const id of ['head', 'keys', 'legend', 'score', 'acts', 'briefing', 'banner', 'denial', 'advisory',
                    'lostlink']){
    ui.run('setMinimized', id, true);
    assert.equal(ui.run('isMinimized', id), true, id);
    ui.run('setMinimized', id, false);
    assert.equal(ui.run('isMinimized', id), false, id);
  }
  assert.deepEqual(JSON.parse(saved['skynet-panels']).keys, false);
  ui.run('setMinimized', 'banner', true);
  const bulletin = {id:'n1', kind:'notam', text:'AREA BOUNDED BY 404310N0735920W SFC-400FT AGL 0907-0912Z',
                    published_tick:525, until_tick:900};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[]});
  assert.match(ui.element('banner').innerHTML, /data-min="banner" aria-expanded="false"[^>]*>\+</);
  assert.match(ui.element('banner').innerHTML, /NOTAM/, 'content is drawn while minimized, so expanding shows it');
  const blocked = scene({localStorage:{getItem(){ throw new Error('blocked'); }, setItem(){ throw new Error('blocked'); }}});
  blocked.run('setMinimized', 'score', true);
  assert.equal(blocked.run('isMinimized', 'score'), true);
  blocked.run('renderSnapshot', snapshot(), {ledger:[], notices:[]});
});

test('a minimized refusal card opens again when a new refusal arrives', () => {
  const ui = scene();
  ui.run('setMinimized', 'denial', true);
  ui.run('renderDenials', {ledger:[denial('fresh-1')]}, 0);
  assert.equal(ui.run('isMinimized', 'denial'), false);
});

test('lines the runtime wrote itself name the runtime, not an internal id', () => {
  const ui = scene();
  const entry = approval('i1', {proposal:{asset_id:'intake', action:'intake', author:'runtime', params:{}},
    decision:{verdict:'auto', reason:'read', code:'intake_read', detail:{kind:'weather', read_by:'grammar'}}});
  ui.run('renderSnapshot', snapshot(), {ledger:[entry], locks:{}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /<b>runtime<\/b>/, feed);
  assert.doesNotMatch(feed, /<b>intake<\/b>/);
});


// ── Runtime briefing, in its real shape: /state.briefing items (status is the standing, trust says whether
//    the source domain is official) and, per rule, a BriefingNotice in the notice book
//    (id = rule_id, polygon, ceiling_m, citation). ──────────────────────────────────────────────────────
function briefing(extra = {}) {
  return {enabled:true, source:'live', mode:'live', runs:1, last_run_tick:120, credits_used:3, budget:20,
    summary:'Two crane permits and one park closure near today’s landing areas.',
    items:[
      {id:'brief-c1', kind:'crane', place:'110th Street Manhattan', summary:'Tower crane permit, 95 m, active all week',
       url:'https://www1.nyc.gov/permit/123', domain:'nyc.gov', trust:'official', status:'applied',
       rule_id:'brief-c1', until_tick:5000},
      {id:'brief-p1', kind:'closure', place:'Morningside Park', summary:'Park closed for a film shoot until 18:00',
       url:'https://www.nycgovparks.org/closure', domain:'nycgovparks.org', trust:'official', status:'applied',
       rule_id:'brief-p1'},
      {id:'brief-e1', kind:'event', place:'Yankee Stadium', summary:'Game tonight, crowds from 18:00',
       url:'https://untrusted.example/news', domain:'untrusted.example', trust:'unofficial', status:'held',
       rule_id:'brief-e1'},
      {id:'brief-i1', kind:'info', place:'East Side', summary:'UN General Assembly week', url:'javascript:alert(1)',
       domain:'example.org', trust:'unofficial', status:'info', rule_id:null},
    ], ...extra};
}
function briefingNotices({eventApplied = false} = {}) {
  const circle = (lat, lon, r) => [[lat + r, lon], [lat, lon + r], [lat - r, lon], [lat, lon - r]];
  const cite = domain => ({source_url:`https://${domain}/x`, title:'t', domain, fetched_at:0, read_by:'grammar',
                           trust:'official', query:'', recorded:false});
  return [
    {id:'brief-c1', name:'CRANE · 110th Street Manhattan · 95 m', kind:'crane', applied:true, held:false,
     source:'grammar', polygon:circle(40.7995, -73.9535, 0.0002), floor_m:0, ceiling_m:95, citation:cite('nyc.gov')},
    {id:'brief-p1', name:'CLOSED · Morningside Park', kind:'closure', applied:true, held:false, source:'grammar',
     polygon:circle(40.805, -73.959, 0.0005), floor_m:0, ceiling_m:-1, citation:cite('nycgovparks.org')},
    {id:'brief-e1', name:'EVENT · Yankee Stadium', kind:'event', applied:eventApplied, held:!eventApplied,
     source:eventApplied ? 'human' : 'grammar', polygon:circle(40.8296, -73.9262, 0.003), floor_m:0,
     ceiling_m:null, citation:cite('untrusted.example')},
  ];
}

test('the runtime briefing panel lists what the runtime read, its source and how far it applies', () => {
  const ui = scene();
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:briefingNotices(), briefing:briefing()});
  const card = ui.element('briefing');
  assert.equal(card.hidden, false);
  assert.match(card.innerHTML, /runtime briefing/, 'the title sits on the same line as the other cards');
  assert.match(card.innerHTML, /data-min="briefing"/, 'the same minimize button');
  assert.match(card.innerHTML, /data-kind="crane"[\s\S]*110th Street Manhattan/);
  assert.match(card.innerHTML,
    /<a href="https:\/\/www1\.nyc\.gov\/permit\/123" target="_blank" rel="noopener noreferrer">nyc\.gov<\/a>/);
  assert.equal((card.innerHTML.match(/>APPLIED</g) || []).length, 2, 'status says what applies, not trust');
  assert.match(card.innerHTML, />WAITING FOR A PERSON</);
  assert.match(card.innerHTML, />INFO ONLY</);
  assert.doesNotMatch(card.innerHTML, /javascript:/, 'only http(s) addresses from the search become links');
  assert.match(card.innerHTML, /briefed at tick 120 · 3 of 20 searches/);
  assert.equal(ui.element('banner').hidden, true, 'a briefing notice is told by the briefing card, not the banner');
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[], briefing:briefing({source:'recorded'})});
  assert.match(ui.element('briefing').innerHTML, /RECORDED/, 'recorded material says it is recorded');
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[], briefing:{enabled:true, source:'recorded', runs:0,
    summary:'no briefing yet.', items:[]}});
  assert.equal(ui.element('briefing').hidden, true, 'never run means no card');
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[]});
  assert.equal(ui.element('briefing').hidden, true, 'no briefing, no card');
});

test('a briefing crane stands on the map, an applied event is painted with its name, a closed landing area goes grey', () => {
  const ui = scene();
  const snap = snapshot();
  snap.worlds.guarded.landing_areas = [{id:'la-morningside', name:'Morningside Park', lat:40.805, lon:-73.959},
                                       {id:'la-pier', name:'Pier 17', lat:40.706, lon:-74.002}];
  ui.run('renderSnapshot', snap, {ledger:[], notices:briefingNotices(), briefing:briefing()});
  const crane = ui.source('brief-crane').features;
  assert.ok(crane.length >= 2, 'a thin mast and the arm on top');
  assert.ok(crane.some(f => Math.abs(f.properties.height - 95) < 0.01), 'the height is the rule ceiling exactly');
  assert.equal(ui.source('brief-crane-label').features[0].properties.label, 'CRANE 95 m · nyc.gov');
  assert.equal(ui.source('zone').features.length, 0,
               'cranes and closures are not red circles, and an event waiting for a person blocks nothing');
  const [closed, open] = ui.source('landing').features.map(f => f.properties);
  assert.equal(closed.closed, true);
  assert.equal(closed.tag, 'CLOSED · nycgovparks.org');
  assert.equal(open.closed, false, 'a landing area outside the circle is untouched');
  assert.equal(ui.source('landing-disc').features[0].properties.closed, true);
  ui.run('renderSnapshot', snap, {ledger:[], notices:briefingNotices({eventApplied:true}), briefing:briefing()});
  const zone = ui.source('zone').features;
  assert.equal(zone.length, 1, 'an event a person confirmed is painted like a zone');
  assert.equal(zone[0].properties.name, 'EVENT · Yankee Stadium');
  assert.match(ui.element('legend').innerHTML, /Crane \(briefing\)[\s\S]*Closed landing area/);
});

test('a refusal caused by a briefing rule names the rule and its source', () => {
  const ui = scene();
  const crane = denial('c1', {proposal:{asset_id:'drone-01', action:'fly_route',
    params:{legs:[start, ...route], blocked_kind:'forbidden', blocked_volume:'brief-c1', blocked_leg:1,
            blocked_ceiling_m:95, blocked_at:{lat:route[0].lat, lon:route[0].lon},
            blocked_polygon:[[40.7995, -73.9535], [40.7996, -73.9535], [40.7996, -73.9534]]}},
    decision:{verdict:'denied', reason:'a crane is in the way', code:'airspace'}});
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:briefingNotices(), briefing:briefing()});
  ui.run('renderDenials', {ledger:[crane]}, 0);
  assert.equal(ui.element('denial-why').textContent, 'leg 1 enters CRANE 95 m (nyc.gov)');
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.match(ui.source('stage-label').features[0].properties.label, /^REJECTED · CRANE 95 m \(nyc\.gov\)/);
  assert.equal(ui.source('blocker').features[0].properties.height, 95, 'a crane stands like a building');
  // Even when it drops off the item list (only the last few are carried), the name comes from the citation
  // in the notice book.
  const later = scene();
  later.run('renderSnapshot', snapshot(), {ledger:[], notices:briefingNotices(), briefing:briefing({items:[]})});
  later.run('renderDenials', {ledger:[crane]}, 0);
  assert.equal(later.element('denial-why').textContent, 'leg 1 enters CRANE 95 m (nyc.gov)');
  // The demo caption names the crane and its source too.
  const demo = demoShot({notices:briefingNotices().slice(0, 1)});
  assert.equal(demo.caption, 'Runtime briefing — CRANE · 110th Street Manhattan · 95 m, from nyc.gov. '
    + 'It applies now: routes through it are refused.');
  assert.ok(Math.abs(demo.fly[1].center[1] - 40.7995) < 1e-9, 'to the crane');
});

// ── What the model did (hover card). It uses params.model_trace only. ──────────────────────────────────
const AGENTS = {'drone-01':{model:'nemotron-3-nano:4b', host:'ollama', world:'guarded', last_seen_tick:1,
                            display:'Nemotron Nano 4B', model_ok:true}};
function traced(trace, params = {}) {
  return approval('tr1', {proposal:{asset_id:'drone-01', action:'fly_route', author:'nemotron-3-nano:4b',
    params:{legs:[start, ...route], drafter:'astar', model_trace:trace, ...params}},
    decision:{verdict:'auto', reason:'within limits', code:'within_limits'}});
}

test('the hover card tells the model’s part in plain words, from the aircraft and from a ledger line', () => {
  const ui = scene();
  const trace = {form:{model:'nemotron-3-nano:4b', action:'fly_route', latency_ms:1820, used:true,
      concern:'has a delivery to Morningside Park and no cleared route',
      rationale:'Battery is full and the park is open.', fallback_reason:''},
    route:{source:'choice', draft:null,
      choice:{candidates:[{id:'a', label:'low route'}, {id:'b', label:'high route'}, {id:'c', label:'river route'}],
              chosen:'a', reason:'weather hold expected'}}};
  ui.run('renderSnapshot', snapshot(), {ledger:[traced(trace)], agents:AGENTS, locks:{}});
  assert.equal(ui.run('showTraceFor', 'drone-01'), true);
  const card = ui.element('trace');
  assert.equal(card.hidden, false);
  assert.match(card.innerHTML, /<b>Nemotron Nano 4B<\/b>/);
  assert.match(card.innerHTML, /1\.8 s/);
  assert.match(card.innerHTML,
    /<dt>Asked<\/dt><dd>has a delivery to Morningside Park and no cleared route<\/dd>/);
  assert.match(card.innerHTML,
    /<dt>Answered<\/dt><dd>delivery route — “Battery is full and the park is open\.”<\/dd>/);
  assert.match(card.innerHTML, /<dt>Code<\/dt><dd>accepted<\/dd>/);
  assert.match(card.innerHTML,
    /<dt>Route<\/dt><dd>the model chose the low route among 3 candidates — “weather hold expected”<\/dd>/);
  assert.match(card.innerHTML, /<dt>Runtime<\/dt><dd>APPROVED — within limits<\/dd>/);
  assert.doesNotMatch(card.innerHTML, /model_trace|[{}]/, 'the raw JSON is never shown');
  // Hovering a ledger row raises the same card, for that row's request form.
  ui.run('hideTrace');
  ui.run('showTraceForRow', {dataset:{entry:'tr1'}});
  assert.equal(ui.element('trace').hidden, false);
  assert.match(ui.element('trace').innerHTML, /<b>Nemotron Nano 4B<\/b>/);
  ui.run('hideTrace', 'feed');
  assert.equal(ui.element('trace').hidden, true, 'the card disappears when the mouse leaves');
});

test('the hover card says when rules wrote the form, and when the model draft failed', () => {
  const rules = scene();
  const noModel = {form:{model:'', concern:'battery at 18% and no charger booked', action:'fly_route',
      rationale:'', latency_ms:null, used:false, fallback_reason:'no model'},
    route:{source:'straight', choice:null, draft:null}};
  rules.run('renderSnapshot', snapshot(), {ledger:[traced(noModel)], locks:{}});
  rules.run('showTraceFor', 'drone-01');
  const plain = rules.element('trace').innerHTML;
  assert.match(plain, /<b>rules<\/b>/);
  assert.match(plain, /<dt>Answered<\/dt><dd>no model answer<\/dd>/);
  assert.match(plain, /<dt>Code<\/dt><dd>rules wrote it — no model<\/dd>/);
  assert.match(plain, /<dt>Route<\/dt><dd>straight line<\/dd>/);
  // A model that was there but did not answer in time is a different thing — the name is that model, and
  // the code says the rules wrote it instead.
  const late = scene();
  const timeout = {form:{model:'', concern:'needs a route', action:'fly_route', rationale:'', latency_ms:9000,
      used:false, fallback_reason:'timeout'}, route:{source:'astar', choice:null, draft:null}};
  late.run('renderSnapshot', snapshot(), {ledger:[traced(timeout)], agents:AGENTS, locks:{}});
  late.run('showTraceFor', 'drone-01');
  assert.match(late.element('trace').innerHTML, /<b>Nemotron Nano 4B<\/b>/);
  assert.match(late.element('trace').innerHTML, /<dt>Code<\/dt><dd>rules wrote it — no answer in time<\/dd>/);
  assert.match(late.element('trace').innerHTML, /<dt>Route<\/dt><dd>A\* \(rules\)<\/dd>/);
  // The rules choosing among candidates — exactly the ledger shape of the second stack (source is astar,
  // with choice alongside).
  const picked = scene();
  const rulesChoice = {form:{model:'', concern:'has a delivery to Sara D. Roosevelt Park, no cleared route',
      action:'fly_route', rationale:'delivering to Sara D. Roosevelt Park, battery 55%', latency_ms:0, used:false,
      fallback_reason:'no model'},
    route:{source:'astar', draft:null, choice:{candidates:[{id:'a', label:'shortest', length_m:2739, max_alt_m:70},
      {id:'b', label:'lowest altitude', length_m:2736, max_alt_m:70}], chosen:'b',
      reason:'rules: the previous candidate was refused (airspace)'}}};
  picked.run('renderSnapshot', snapshot(), {ledger:[traced(rulesChoice, {route_choice:{chosen:'b', path:'rules',
    model:'', reason:'rules: the previous candidate was refused (airspace)', candidates:[]}})], locks:{}});
  picked.run('showTraceFor', 'drone-01');
  const chosen = picked.element('trace').innerHTML;
  assert.match(chosen, /<dt>Route<\/dt><dd>rules chose the lowest altitude among 2 candidates<\/dd>/);
  assert.doesNotMatch(chosen, /0\.0 s/, 'a form the rules wrote carries no latency');
  // The model draft went through a building, so A* drew the route.
  const drafted = scene();
  const failed = {form:{model:'nemotron-3-nano:4b', concern:'refused once, needs a new route', action:'fly_route',
      rationale:'Go around the block to the west.', latency_ms:900, used:true, fallback_reason:''},
    route:{source:'astar', choice:null,
      draft:{asked:true, latency_ms:4200, breach:'crossed bldg-t02452, roof 114 m', used:false}}};
  drafted.run('renderSnapshot', snapshot(), {ledger:[traced(failed)], agents:AGENTS, locks:{}});
  drafted.run('showTraceFor', 'drone-01');
  assert.match(drafted.element('trace').innerHTML,
    /<dt>Route<\/dt><dd>model draft failed — it crossed a 114 m building; A\* drew it<\/dd>/);
});

test('an older line without a trace shows only who wrote and who drew it, in either language', () => {
  const ui = scene({localStorage:{getItem:key => key === 'skynet-lang' ? 'ko' : null, setItem(){}}});
  ui.run('renderSnapshot', snapshot(), {ledger:[approval('old1', {proposal:{asset_id:'drone-01',
    action:'fly_route', author:'rules', params:{legs:[start, ...route], drafter:'astar'}},
    decision:{verdict:'auto', reason:'within limits', code:'within_limits'}})], locks:{}});
  ui.run('showTraceFor', 'drone-01');
  const card = ui.element('trace').innerHTML;
  assert.match(card, /<dt>신청서 작성<\/dt><dd>규칙<\/dd>/);
  assert.match(card, /<dt>경로 작성<\/dt><dd>A\*<\/dd>/);
  assert.doesNotMatch(card, /물음/, 'no trace means no asked and answered');
});

test('the hover card stands beside the aircraft, never on top of it', () => {
  const ui = scene();
  const size = {w:300, h:190};
  const wide = {w:1500, h:940, reserveRight:302};
  assert.equal(ui.run('placeTrace', {x:400, y:300}, size, wide).left, 456, 'it steps aside to the aircraft\'s right');
  assert.equal(ui.run('placeTrace', {x:1100, y:300}, size, wide).left, 744, 'to the left when the right is tight');
  for (const [anchor, view] of [[{x:400, y:300}, wide], [{x:1100, y:300}, wide],
                                [{x:700, y:60}, {w:760, h:400, reserveRight:0}]]){
    const at = ui.run('placeTrace', anchor, size, view);
    const covers = anchor.x >= at.left && anchor.x <= at.left + size.w
                && anchor.y >= at.top && anchor.y <= at.top + size.h;
    assert.ok(!covers, `it covers the aircraft ${JSON.stringify(at)}`);
    assert.ok(at.left >= 12 && at.top >= 12, JSON.stringify(at));
  }
});

// ── Route choice. The code draws the candidates; the model only picks one. ─────────────────────────────
test('a route the model chose says who chose it and why, on the corridor and in the ledger line', () => {
  const ui = scene();
  const choice = {candidates:[
      {id:'a', label:'low route', legs_count:4, length_m:2310, max_alt_m:75, min_alt_m:60, reason_tags:['low']},
      {id:'b', label:'high route', legs_count:3, length_m:2100, max_alt_m:120, min_alt_m:110, reason_tags:['fast']}],
    chosen:'a', reason:'weather hold expected', model:'nemotron-3-nano:4b', path:'tools'};
  const entry = approval('rc1', {proposal:{asset_id:'drone-01', action:'fly_route', author:'nemotron-3-nano:4b',
    params:{legs:[start, ...route], drafter:'choice:nemotron-3-nano:4b', route_choice:choice}},
    decision:{verdict:'auto', reason:'within limits', code:'within_limits'}});
  ui.run('renderSnapshot', snapshot(), {ledger:[entry], agents:AGENTS, locks:{}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /delivery route · model choice/);
  assert.match(feed, /Nemotron Nano 4B chose the low route — weather hold expected/);
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.equal(ui.source('stage-label').features[0].properties.label,
    'APPROVED · Nemotron Nano 4B chose the low route — weather hold expected');
  const card = ui.run('assetCard',
    {id:'drone-01', state:'ready', battery:80, alt_m:0, route:[], delivered:0});
  assert.match(card, /<li class="chosen">low route · 4 legs · 2\.3 km · 60–75 m<\/li>/);
  assert.match(card, /<li class="">high route · 3 legs · 2\.1 km · 110–120 m<\/li>/);
  // A form with no candidates is unchanged.
  const plain = scene();
  plain.run('renderSnapshot', snapshot(), {ledger:[approval('p1', {decision:{verdict:'auto', reason:'within limits',
    code:'within_limits'}})], locks:{}});
  plain.time(GROW_MS + CHECK_MS + 10); plain.run('draw');
  assert.equal(plain.source('stage-label').features[0].properties.label, 'APPROVED · within limits');
});

// ── The runtime and the links. One dotted line per cleared aircraft, up to the runtime mast. ───────────
test('an aircraft has a dotted link to the runtime mast, and a lost link is grey and broken', () => {
  const ui = scene();
  const snap = snapshot(2, 1, route, {lon:-73.97, lat:40.705, alt_m:110, state:'delivering'});
  ui.run('renderSnapshot', snap, {ledger:[], notices:[]});
  ui.time(700); ui.run('draw');
  const lines = ui.source('signal-line').features;
  assert.ok(lines.length > 3, 'a dotted line is many pieces');
  assert.ok(lines.every(f => f.properties.asset === 'drone-01' && f.properties.kind === 'line'));
  assert.ok(lines.every(f => f.properties.height > f.properties.base));
  // The mast rises from the roof of 26 Federal Plaza (about 240 m) to the link altitude.
  const mast = ui.source('runtime').features;
  assert.ok(mast.length >= 1, 'the mast is drawn');
  assert.ok(mast.some(f => f.properties.base >= geometry.RUNTIME_ROOF_M - 1), 'it starts at the roof');
  assert.ok(mast.some(f => f.properties.height >= geometry.RUNTIME_ALT_M), 'it rises to the link altitude');
  ui.run('renderSnapshot', snapshot(3, 1, route, {lon:-73.97, lat:40.706, alt_m:110, state:'delivering'}),
    {ledger:[], notices:[], links:{'drone-01':{status:'lost', since_tick:400, last_seen_tick:399}}});
  ui.time(1000); ui.run('draw');
  const broken = ui.source('signal-line').features;
  assert.ok(broken.length && broken.every(f => f.properties.kind === 'lost'), 'a lost link is a grey line');
  assert.ok(broken.length < lines.length, 'the middle is empty, so there are fewer pieces');
  assert.match(ui.element('legend').innerHTML, /SKY-NET RUNTIME[\s\S]*lost link/);
  assert.match(ui.element('legend').innerHTML, /26 Federal Plaza \(a federal government building\)/);
  assert.doesNotMatch(ui.element('legend').innerHTML, /TOWER/);
});

// Link lines are background — the corridors and the aircraft are the subject. Half the old opacity, a paler
// blue. These layers are added inside map.on("load"), where the harness cannot catch them, so the
// declarations are read as text.
test('idle link lines are pale, and the event pulses that travel them stay readable', () => {
  const html = readFileSync(new URL('../frontend/map.html', import.meta.url), 'utf8');
  const paintOf = id => {
    const block = html.slice(html.indexOf(`map.addLayer({id:"${id}"`));
    return {opacity:Number(block.match(/"fill-extrusion-opacity":([\d.]+)/)[1]),
            colour:block.match(/"fill-extrusion-color":"(#[0-9a-f]{6})"/)?.[1]};
  };
  assert.ok(paintOf('signal-line').opacity <= 0.25, 'an idle link is at most half the old .5');
  assert.ok(paintOf('signal-lost').opacity <= 0.3, 'a lost link is pale too');
  assert.notEqual(paintOf('signal-line').colour, '#3d8bff', 'the idle colour is a paler blue');
  assert.ok(paintOf('signal-dot').opacity >= 0.9, 'the travelling dots stay crisp');
});

test('a filing travels up the line and the verdict comes back down, a recall comes down red', () => {
  const ui = scene();
  ui.run('renderSnapshot', snapshot(), {ledger:[denial('sig-1')], notices:[]});
  ui.time(600); ui.run('draw');
  const filing = ui.source('signal-dot').features;
  assert.equal(filing.length, 1, 'one filing = one dot going up');
  assert.equal(filing[0].properties.colour, '#ffd23f');
  assert.equal(filing[0].properties.kind, 'dot');
  ui.time(GROW_MS + CHECK_MS - 100); ui.run('draw');
  const verdict = ui.source('signal-dot').features;
  assert.equal(verdict.length, 1, 'a verdict is one dot coming down');
  assert.equal(verdict[0].properties.colour, '#ff3b30', 'a refusal is red');
  // A recall is something the runtime sends down.
  const recall = {id:'rc9', at:Date.now() / 1000, outcome:'done',
    proposal:{asset_id:'drone-01', action:'divert_ground', author:'runtime', params:{volume:'nofly-1'}},
    decision:{verdict:'auto', reason:'route in flight recalled by nofly-1', code:'recalled', policy_hit:'nofly-1',
              detail:{resource:'nofly-1', policy:'nofly-1'}}};
  ui.run('renderSnapshot', snapshot(), {ledger:[recall, denial('sig-1')], notices:[]});
  ui.time(GROW_MS + CHECK_MS + 400); ui.run('draw');
  const down = ui.source('signal-dot').features;
  assert.ok(down.some(f => f.properties.colour === '#ff3b30'), 'a recall comes down red');
});

// ── The demo director (?demo=1) ────────────────────────────────────────────────────────────────────────
function demoScene(overrides = {}) {
  return scene({location:{hostname:'localhost', search:'?demo=1'}, URLSearchParams, ...overrides});
}
function demoShot(runtime, snap = snapshot()) {
  const ui = demoScene();
  ui.run('renderSnapshot', snap, {ledger:[], notices:[], ...runtime});
  ui.time(50); ui.run('draw');
  return {ui, fly:ui.calls.filter(c => c[0] === 'flyTo').at(-1), caption:ui.element('caption-text').textContent};
}

test('the demo director only runs with ?demo=1', () => {
  const ui = scene();
  ui.run('renderSnapshot', snapshot(), {ledger:[denial('off-1')], notices:[]});
  ui.time(200); ui.run('draw');
  assert.equal(ui.calls.filter(c => c[0] === 'flyTo').length, 0,
               'on the normal screen the camera never moves by itself');
  assert.equal(ui.element('caption').hidden, true);
});

test('a refusal flies the camera to the aircraft and the building, with a caption built from ledger values', () => {
  const ui = demoScene();
  const blocked = {lat:40.7075, lon:-73.9695};
  const refused = denial('cap-1', {proposal:{asset_id:'drone-01', action:'fly_route',
    params:{legs:[start, ...route], drafter:'straight', blocked_kind:'forbidden', blocked_volume:'bldg-t1',
            blocked_leg:1, blocked_ceiling_m:114, blocked_at:blocked}},
    decision:{verdict:'denied', reason:'a building is in the way', code:'airspace'}});
  const snap = snapshot();
  snap.worlds.guarded.assets['drone-01'].job = 'Harlem';
  ui.run('renderSnapshot', snap, {ledger:[refused], notices:[]});
  ui.time(100); ui.run('draw');
  const fly = ui.calls.filter(c => c[0] === 'flyTo').at(-1);
  assert.ok(fly, 'the camera moves');
  assert.ok(Math.abs(fly[1].center[1] - (start.lat + blocked.lat) / 2) < 1e-6, 'between the aircraft and the building');
  assert.ok(fly[1].zoom > 14 && fly[1].zoom <= 16.8, `zoom ${fly[1].zoom}`);
  assert.equal(ui.element('caption-text').textContent,
    'drone-01 filed a straight line to Harlem — it clips a 114 m building. Refused.');
  assert.equal(ui.element('caption').hidden, false);
  // Once there has been time to read it, the camera returns to the wide view and the caption goes down.
  ui.time(7300); ui.run('draw');
  assert.equal(ui.calls.at(-1)[1].zoom, 14.5, 'back to the wide view');
  assert.equal(ui.element('caption-text').textContent, '');
});

test('scheduled scenes take the camera to the zone, the warehouse, the fire circle and the dark aircraft', () => {
  const ring = [[40.81, -73.94], [40.81, -73.93], [40.82, -73.93]];
  const notam = demoShot({notices:[{id:'n1', name:'Harlem TFR', applied:true, held:false, source:'grammar',
    from_tick:525, until_tick:900, polygon:ring}]});
  assert.ok(Math.abs(notam.fly[1].center[1] - 40.815) < 1e-9, 'to the middle of the zone (between its extremes)');
  assert.match(notam.caption, /^NOTAM · Harlem TFR, tick 525–900 — read by the rule grammar\./);

  const weather = demoShot({weather:{hold:{id:'wx1', reason:'WEATHER HOLD · gusts 14 m/s > 12',
    until_tick:2700, since_tick:2200, source:'grammar'}, held:[]}});
  assert.ok(Math.abs(weather.fly[1].center[1] - start.lat) < 1e-6, 'the warehouse is centred');
  assert.equal(weather.caption, 'WEATHER HOLD · gusts 14 m/s > 12 — takeoffs held until tick 2700. '
    + 'Aircraft already in the air continue to land.');

  const fire = demoShot({incidents:[{id:'f1', name:'FIRE · 4705 Center Boulevard', kind:'fire',
    centre:[40.745618, -73.956797], radius_m:150, until_tick:3600, applied:true, held:false}]});
  assert.deepEqual(fire.fly[1].center, [-73.956797, 40.745618]);
  assert.match(fire.caption, /^FIRE · 4705 Center Boulevard — 150 m keep-out until tick 3600\./);

  const lost = demoShot({links:{'drone-01':{status:'lost', since_tick:3800, last_seen_tick:3799}}});
  assert.deepEqual(lost.fly[1].center, [start.lon, start.lat]);
  assert.equal(lost.fly[1].zoom, 16.2);
  assert.equal(lost.caption, 'drone-01 lost its link at tick 3800. The runtime keeps its filed space '
    + 'reserved — nobody else may enter it.');

  const advised = demoShot({advisories:[advisory({asset:'drone-01', ledger_id:'adv-9'})]});
  assert.deepEqual(advised.fly[1].center, [start.lon, start.lat]);
  assert.equal(advised.caption, 'After 3 refusals in a row the runtime suggests to drone-01: '
    + 'hold on the ground until tick 700. Information only — the operator decides.');
});

test('a drag pauses the director for 20 s, and the scene it missed plays when the pause ends', () => {
  const ui = demoScene();
  ui.run('userMoved', {originalEvent:{}});
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[], weather:{hold:{id:'wx1',
    reason:'WEATHER HOLD · gusts 14 m/s > 12', until_tick:2700, since_tick:2200, source:'grammar'}, held:[]}});
  ui.time(1000); ui.run('draw');
  assert.equal(ui.calls.filter(c => c[0] === 'flyTo').length, 0, 'still while a person holds the map');
  assert.match(ui.element('caption-meta').textContent, /director paused/);
  ui.time(21000); ui.run('draw');
  assert.equal(ui.calls.filter(c => c[0] === 'flyTo').length, 1, 'after the pause, that scene first');
  assert.match(ui.element('caption-text').textContent, /^WEATHER HOLD/);
});

// ── The PX4 SITL mirror ────────────────────────────────────────────────────────────────────────────────
test('a PX4 mirror draws a ghost with its mode and mission step; without the field nothing is drawn', () => {
  const ui = scene();
  const pilot = {lat:40.705, lon:-73.968, alt_m:60, armed:true, mode:'AUTO.MISSION', mission_seq:3,
                 endpoint:'udp://127.0.0.1:14540', last_heartbeat_s:0.4};
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[], autopilots:{'drone-01':pilot}});
  ui.time(100); ui.run('draw');
  assert.equal(ui.source('px4').features.length, 5, 'one body and four rotors');
  assert.ok(ui.source('px4').features.every(f => f.properties.height > f.properties.base));
  assert.equal(ui.source('px4-label').features[0].properties.label,
               'PX4 SITL · drone-01\nAUTO.MISSION · step 3 · armed');
  assert.match(ui.element('legend').innerHTML, /PX4 SITL mirror/);
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[]});
  ui.time(200); ui.run('draw');
  assert.equal(ui.source('px4').features.length, 0, 'a missing field draws nothing');
  assert.equal(ui.source('px4-label').features.length, 0);
});

// A screen word must have the same slots (%s) in both languages — captions and cards fill values in order.
test('every screen text key exists in both languages with the same number of placeholders', () => {
  const html = readFileSync(new URL('../frontend/map.html', import.meta.url), 'utf8');
  const TEXT = vm.runInNewContext(`(${html.match(/const TEXT = (\{[\s\S]*?\n\});/)[1]})`);
  const holes = text => (String(text).match(/%s/g) || []).length;
  const missing = Object.keys(TEXT.en).filter(key => !(key in TEXT.ko));
  assert.deepEqual(missing, [], `words with no Korean: ${missing.join(', ')}`);
  const uneven = Object.keys(TEXT.en).filter(key => holes(TEXT.en[key]) !== holes(TEXT.ko[key]));
  assert.deepEqual(uneven, [], `words with a different slot count: ${uneven.join(', ')}`);
});

test('captions speak Korean with the language toggle', () => {
  const ui = demoScene({localStorage:{getItem:key => key === 'skynet-lang' ? 'ko' : null, setItem(){}}});
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[],
    links:{'drone-01':{status:'lost', since_tick:3800, last_seen_tick:3799}}});
  ui.time(50); ui.run('draw');
  assert.equal(ui.element('caption-text').textContent,
    'drone-01 링크 두절 (틱 3800). 런타임은 그 기체가 낸 공간을 그대로 잡아 둡니다 — 아무도 못 들어갑니다.');
});

test('a recall caption names the rule that pulled the route, not the aircraft', () => {
  const ui = demoScene();
  // A notice confirmed but not yet applied — no scene of its own, but the name comes from the notice book.
  const tfr = {id:'nofly-1', name:'Harlem TFR', applied:false, held:false, source:'human', from_tick:1350,
               until_tick:2100, polygon:[[40.81, -73.94], [40.81, -73.93], [40.82, -73.93]]};
  const recall = {id:'rc-live', at:Date.now() / 1000, outcome:'done',
    proposal:{asset_id:'drone-01', action:'divert_ground', author:'runtime', params:{volume:'nofly-1'}},
    decision:{verdict:'auto', reason:'route in flight recalled by nofly-1', code:'recalled', policy_hit:'nofly-1',
              detail:{resource:'drone-01', policy:'nofly-1'}}};
  ui.run('renderSnapshot', snapshot(), {ledger:[recall], notices:[tfr]});
  ui.time(50); ui.run('draw');
  assert.equal(ui.element('caption-text').textContent,
    'A rule arrived — drone-01’s approved route was pulled back (Harlem TFR). It holds until a new one is approved.'
      .replace('’', "'"));
});

// Recorded material (hand-written scenes, answers fetched earlier) says it is recorded not only on the
// briefing card but on the map, in what-blocked-it and in the caption. Without that, a made-up notice
// would stand on screen in the same words as one searched today.
test('recorded briefing material says so on the map, in what-blocked-it and in the demo caption', () => {
  const recordedNotices = briefingNotices().map(n => ({...n, citation:{...n.citation, recorded:true}}));
  const recorded = briefing({source:'recorded'});
  recorded.items = recorded.items.map(item => ({...item, recorded:true}));
  const ui = scene();
  const snap = snapshot();
  snap.worlds.guarded.landing_areas = [{id:'la-morningside', name:'Morningside Park', lat:40.805, lon:-73.959}];
  ui.run('renderSnapshot', snap, {ledger:[], notices:recordedNotices, briefing:recorded});
  assert.equal(ui.source('brief-crane-label').features[0].properties.label, 'CRANE 95 m · nyc.gov · recorded');
  assert.equal(ui.source('landing').features[0].properties.tag, 'CLOSED · nycgovparks.org · recorded');
  const blocked = {blocked_volume:'brief-c1', blocked_kind:'forbidden', blocked_ceiling_m:95};
  assert.equal(ui.run('blockedLabel', blocked), 'CRANE 95 m (nyc.gov · recorded)');
  assert.equal(ui.run('blockPhrase', blocked), 'it clips a 95 m crane (nyc.gov · recorded)');
  assert.match(ui.run('briefingShot', recordedNotices[0]).caption(), /, from nyc\.gov · recorded\. It applies now/);
  // Even off the item list (left only in the notice book), recorded stays recorded.
  ui.run('renderSnapshot', snap, {ledger:[], notices:recordedNotices, briefing:{...recorded, items:[]}});
  assert.equal(ui.run('blockedLabel', blocked), 'CRANE 95 m (nyc.gov · recorded)');
  // A live source shows the domain only.
  ui.run('renderSnapshot', snap, {ledger:[], notices:briefingNotices(), briefing:briefing()});
  assert.equal(ui.source('brief-crane-label').features[0].properties.label, 'CRANE 95 m · nyc.gov');
  assert.equal(ui.run('blockedLabel', blocked), 'CRANE 95 m (nyc.gov)');
});

test('the approval screen says when a held notice comes from a recorded briefing, not a live search', async () => {
  const world = {assets:{}, pads:{}, scoreboard:{spend_usd:0, human_approvals:0}, fleet_limit:450, events:[]};
  const compare = {tick:10, recall_tick:null, worlds:{guarded:world, direct:structuredClone(world)}};
  const card = isRecorded => ({id:`n-${isRecorded}`, asset_id:'airspace', action:'publish_notice', cost_usd:0,
    blast_radius:'none', rationale:'EVENT · Union Square — march',
    params:{notice:{id:'brief-rec-1', citation:{domain:'eastvillage-bulletin.example', recorded:isRecorded}}}});
  const state = {llm:{enabled:false, models:{}}, ledger:[], incidents:[], weather:{}, links:{},
                 awaiting_human:[card(true)]};
  const ui = await approvals(state, compare);
  assert.match(ui.element('inbox').innerHTML,
    /airspace notice[^<]*· <span class="rec">recorded · not a live search<\/span>/);
  const live = await approvals({...state, awaiting_human:[card(false)]}, compare);
  assert.doesNotMatch(live.element('inbox').innerHTML, /recorded/);
});

// ── What the screen says: one runtime world, the word runtime, a real address, two languages ───────────
const HANGUL = /[ᄀ-ᇿ㄰-㆏가-힣]/;
const mapHtml = () => readFileSync(new URL('../frontend/map.html', import.meta.url), 'utf8');
const dictionaries = () => {
  const html = mapHtml();
  return vm.runInNewContext(`(${html.match(/const TEXT = (\{[\s\S]*?\n\});/)[1]})`);
};

// From the header card to the approval page. serve.py serves the map AT the root, so the address
// stays http://localhost:3100 — no redirect, no "/map.html" to type.
test('the map header links to the manual approval screen, and that screen links back', () => {
  const html = mapHtml();
  assert.match(html, /<a href="approvals\.html" data-t="approvals"><\/a>/, 'the link on the header card');
  assert.doesNotMatch(html, /href="index\.html"/, 'index.html is gone now');
  const TEXT = dictionaries();
  assert.equal(TEXT.en.approvals, 'manual approval →');
  assert.equal(TEXT.ko.approvals, '수동 승인 →');
  const inbox = readFileSync(new URL('../frontend/approvals.html', import.meta.url), 'utf8');
  assert.match(inbox, /<a href="map\.html"/, 'the way back to the map from the approval screen');
  const serve = readFileSync(new URL('../frontend/serve.py', import.meta.url), 'utf8');
  assert.match(serve, /ROOT_PATHS = \{"\/", "\/index\.html"\}/);
  assert.match(serve, /MAP_PAGE = "\/map\.html"/);
  assert.doesNotMatch(serve, /send_response\(30\d\)/, 'the root is served, not redirected');
});

// The label is two lines: the name, and under it in small type what it is.
test('the runtime marker label is two lines, and no screen word calls it a tower', () => {
  const ui = scene();
  assert.equal(ui.run('runtimeLabelHtml'),
    '<div>SKY-NET RUNTIME</div><div class="runtime-sub">drone agent runtime</div>');
  const ko = scene({localStorage:{getItem:key => key === 'skynet-lang' ? 'ko' : null, setItem(){}}});
  assert.equal(ko.run('runtimeLabelHtml'),
    '<div>SKY-NET 런타임</div><div class="runtime-sub">드론 에이전트 런타임</div>');
  const TEXT = dictionaries();
  const towers = Object.keys(TEXT.en).filter(key => /\btowers?\b/i.test(String(TEXT.en[key])));
  assert.deepEqual(towers, [], `words that still say tower: ${towers.join(', ')}`);
  const towerKo = Object.keys(TEXT.ko).filter(key => /관제/.test(String(TEXT.ko[key])));
  assert.deepEqual(towerKo, [], `korean words that still say tower: ${towerKo.join(', ')}`);
});

// The runtime stands at a real address — 26 Federal Plaza, the federal building on Foley Square.
test('the runtime stands at 26 Federal Plaza, and the legend says so in both languages', () => {
  assert.deepEqual(geometry.RUNTIME_SITE, {name:'26 Federal Plaza', lat:40.71537, lon:-74.00421});
  assert.equal(geometry.RUNTIME_ALT_M, 600);
  // The mast clears the building (about 240 m) up to the link altitude, with the label at its top.
  const mast = geometry.runtimeMast();
  assert.ok(Math.max(...mast.map(p => p.base + p.height)) >= 600, 'it rises to the link altitude');
  assert.ok(mast.some(p => p.base >= 240 - 1e-9), 'it starts at the roof (240 m)');
  const ui = scene();
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[]});
  assert.match(ui.element('legend').innerHTML,
    /The sky-net runtime, drawn at 26 Federal Plaza \(a federal government building\)\./);
  assert.match(ui.element('legend').innerHTML,
    /Dotted lines are live links; grey and broken is a lost link\./);
  const ko = scene({localStorage:{getItem:key => key === 'skynet-lang' ? 'ko' : null, setItem(){}}});
  ko.run('renderSnapshot', snapshot(), {ledger:[], notices:[]});
  assert.match(ko.element('legend').innerHTML, /26 Federal Plaza\(연방 정부 청사\)에 그려 둔 자리/);
});

// With EN selected the map code emits not one Korean character. Even the comments are English.
test('with EN selected nothing the map writes is Korean, and the two dictionaries match key for key', () => {
  const TEXT = dictionaries();
  assert.deepEqual(Object.keys(TEXT.en).sort(), Object.keys(TEXT.ko).sort(), 'the two dictionaries share their keys');
  const korean = Object.keys(TEXT.en).filter(key => HANGUL.test(String(TEXT.en[key])));
  assert.deepEqual(korean, [], `korean inside the english dictionary: ${korean.join(', ')}`);
  // Outside the dictionaries (comments, markup, CSS) no language's letters are baked in.
  const html = mapHtml();
  const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1];
  const koStart = script.indexOf('\n  ko: {');
  const koEnd = script.indexOf('\n};', koStart);
  assert.ok(koStart > 0 && koEnd > koStart, 'found the korean dictionary');
  const outside = script.slice(0, koStart) + script.slice(koEnd);
  assert.doesNotMatch(outside, HANGUL, 'korean outside the korean dictionary — comments in english too');
  assert.doesNotMatch(html.replace(/<script type="module">[\s\S]*?<\/script>/, ''), HANGUL,
                      'no korean in the markup or the CSS either');
});

// <html lang> and the title follow the chosen language. The default is English.
test('the page language and title follow the language toggle', () => {
  const en = scene({document:{documentElement:{lang:'en'}}});
  assert.equal(en.get('document').title, 'sky-net — runtime map');
  assert.equal(en.get('document').documentElement.lang, 'en');
  const ko = scene({document:{documentElement:{lang:'en'}},
    localStorage:{getItem:key => key === 'skynet-lang' ? 'ko' : null, setItem(){}}});
  assert.equal(ko.get('document').title, 'sky-net — 런타임 지도');
  assert.equal(ko.get('document').documentElement.lang, 'ko');
  assert.match(mapHtml(), /<html lang="en">/, 'the default before the script runs is English too');
});

// ── A briefing run is something the runtime did. Not a refusal, not an approval, and not aircraft link
//    traffic. ───────────────────────────────────────────────────────────────────────────────────────────
function briefingRun(id, detail = {}, extra = {}) {
  return {id, at:Date.now() / 1000, outcome:'noted',
    proposal:{asset_id:'briefing', action:'briefing_run', author:'runtime', params:{}},
    decision:{verdict:'auto', reason:'briefed', code:'briefing_run',
              detail:{places:['Morningside Park'], pages:3, source:'recorded', ...detail}}, ...extra};
}

test('a briefing run is one neutral runtime line — never a refusal, an approval or aircraft traffic', () => {
  const ui = scene();
  const rows = [
    briefingRun('bf1'),
    briefingRun('bf2', {places:['East Side'], pages:2, source:'live'}),
    {id:'bi1', at:Date.now() / 1000, outcome:'noted',
     proposal:{asset_id:'briefing', action:'briefing_item', author:'runtime', params:{}},
     decision:{verdict:'auto', reason:'x', code:'briefing_item', detail:{kind:'crane', domain:'nyc.gov'}}},
    {id:'br1', at:Date.now() / 1000, outcome:'noted',
     proposal:{asset_id:'briefing', action:'briefing_rule', author:'runtime', params:{rationale:'CRANE 95 m'}},
     decision:{verdict:'auto', reason:'x', code:'briefing_rule', detail:{rule:'brief-c1'}}},
  ];
  ui.run('renderSnapshot', snapshot(), {ledger:rows, llm:{enabled:false, models:{}}, locks:{}, notices:[]});
  const feed = ui.element('feed').innerHTML;
  // Just one line — the most recent run. Items and rules are told by the briefing card.
  assert.equal((feed.match(/data-asset="briefing"/g) || []).length, 1);
  assert.match(feed, /<span class="tag t-info">NOTE<\/span>\n\s*<b>runtime<\/b> briefing/);
  assert.match(feed, /Morningside Park · 3 pages \(recorded\)/);
  assert.doesNotMatch(feed, /briefing page|briefing rule/);
  // Neither a refusal nor an approval: no card, and no corridor replayed.
  ui.run('renderDenials', {ledger:rows}, 0);
  assert.equal(ui.element('denial').hidden, true, 'a briefing is not a refusal card');
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.equal(ui.source('stage-label').features.length, 0, 'a briefing is not a corridor');
  assert.equal(ui.layer('flightpath:briefing'), undefined, 'a briefing has no corridor layer');
  // It does not ride the aircraft links either — briefing is not an aircraft.
  assert.equal(ui.source('signal-dot').features.length, 0, 'a briefing is not an aircraft signal');
  // A failed search is still a note, not a refusal.
  const failed = scene();
  failed.run('renderSnapshot', snapshot(), {ledger:[briefingRun('bf3', {source:'live'},
    {outcome:'failed', decision:{verdict:'denied', reason:'x', code:'briefing_run',
      detail:{places:['East Side'], pages:0, source:'live'}}})],
    llm:{enabled:false, models:{}}, locks:{}, notices:[]});
  assert.match(failed.element('feed').innerHTML, /<span class="tag t-info">NOTE<\/span>/);
  assert.doesNotMatch(failed.element('feed').innerHTML, /t-deny/);
  assert.match(failed.element('feed').innerHTML, /East Side · search failed/);
  failed.run('renderDenials', {ledger:[briefingRun('bf3', {}, {outcome:'failed',
    decision:{verdict:'denied', reason:'x', code:'briefing_run', detail:{}}})]}, 0);
  assert.equal(failed.element('denial').hidden, true, 'a failed search is not a refusal card either');
});

// Every decision code the runtime writes has words in both languages — a missing one puts "r_xxx" on screen.
test('every decision code the runtime writes has a screen line in both languages', () => {
  const html = mapHtml();
  const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1];
  const TEXT = dictionaries();
  const cases = new Set([...script.matchAll(/case "([a-z_]+)":/g)].map(m => m[1]));
  const codes = ['advisory', 'airspace', 'airspace_not_loaded', 'briefing_item', 'briefing_rule',
    'briefing_run', 'card_lapsed', 'contingency_unknown', 'duplicate', 'human_action', 'human_blast',
    'human_lift', 'human_lost_link', 'human_notice', 'human_weather', 'incident_keepout',
    'intake_read', 'intake_received', 'intake_source_failed', 'intake_source_recovered',
    'intake_unreadable', 'invalid', 'lift_refused', 'link_lost', 'link_restored', 'lost_link_kept',
    'lost_link_refused', 'lost_link_released', 'nonconforming', 'notice_lapsed', 'notice_published',
    'notice_refused', 'notice_unreadable', 'over_asset', 'over_fleet', 'policy', 'recalled',
    'resource_granted', 'resource_held', 'weather_confirmed', 'weather_hold', 'weather_hold_closed',
    'weather_hold_expired', 'weather_hold_lifted', 'weather_lapsed', 'weather_refused', 'withdrawn',
    'within_limits'];
  const uncovered = codes.filter(code =>
    !cases.has(code) && !(`r_${code}` in TEXT.en && `r_${code}` in TEXT.ko));
  assert.deepEqual(uncovered, [], `codes with no screen words: ${uncovered.join(', ')}`);
});
