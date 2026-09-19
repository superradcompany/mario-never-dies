const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const source = fs.readFileSync(require.resolve('../web/app.js'), 'utf8');

// Exercise the actual controller/render functions without starting canvas animations.
// The browser smoke test uses real recordings; these tests focus on asynchronous races.
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `missing app section: ${start}`);
  return source.slice(from, to);
}
function page() {
  const elements = new Map(), requests = [], listeners = {}, intervals = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {hidden: true, disabled: false, textContent: '', innerHTML: '',
      classList: {toggle() {}}, replaceChildren() {}, setAttribute() {}});
    return elements.get(id);
  };
  const state = {status: 'idle', mode: 'live', busy: false, stopping: false, version: 0, ui: 'test',
    output: null, game: 'mario', events: [], timelines: [], elapsed: 0, live_enabled: true, key_configured: true};
  const context = vm.createContext({
    $, crypto: webcrypto, console, setTimeout() {}, setInterval(fn) { intervals.push(fn); },
    document: {hidden: false, body: {classList: {toggle() {}}}, addEventListener(name, fn) { listeners[name] = fn; }},
    window: {addEventListener(name, fn) { listeners[name] = fn; }}, location: {reload() {}},
    fetch: async (url, options) => { requests.push({url, ...JSON.parse(options.body)}); return {ok: true, json: async () => ({ok: true})}; },
    GAMES: {mario: {}}, ENDED: new Set(['stopped', 'failed', 'complete', 'cleanup_failed']),
    state, elements, requests, listeners, intervals, Boolean, Date,
  });
  function $(id) { return element(id); }
  vm.runInContext(`
    let view = state, run = null, busyRequest = false, watched = false, chosen = true;
    let theater = false, game = 'mario', featured = null, recordings = [], allRuns = [], forkFocus, dockTab;
    const gated = () => false, esc = x => x, gameOf = x => x.game;
    const freshRun = output => ({output, counts: {}, lastEventT: 0, playedT: 0, ops: []});
    const G = () => ({goal: x => x});
    const setTheater = () => {}, loadRuns = () => {}, closeLibrary = () => {}, applyGame = () => {}, syncAttract = () => {};
    const loadSchedule = () => {}, renderPeek = () => {}, tally = () => {}, opsFor = () => [], renderOps = () => {}, catchUp = () => {};
    const enqueue = () => {}, updateCursor = () => {}, canonHop = () => {}, renderPlayback = () => {}, renderMap = () => {}, syncScreen = () => {};
    const setFocus = text => $('focus').textContent = text;
  `, context);
  for (const [start, end] of [
    ['async function command(', '// ---------------------------------------------------------------- the lobby'],
    ['let lobbyShown =', '// Seeks never'],
    ['let leaving = false;', "$('back').onclick"],
    ['let uiVersion = null;', '// State arrives by server-sent events'],
  ]) vm.runInContext(section(start, end), context);
  const evaluate = code => vm.runInContext(code, context);
  const render = overrides => { context.next = {...state, ...overrides}; evaluate('render(next)'); };
  return {context, evaluate, render, element, requests, listeners, intervals};
}

test('leaving keeps the player visible until cleanup completes, then a new run opens', async () => {
  const p = page();
  p.render({busy: true, status: 'running', output: 'first', version: 1});
  p.context.fetch = async () => ({ok: true, json: async () => ({state: {...p.context.next, stopping: true, version: 2}})});
  await p.evaluate('leave()');
  assert.equal(p.element('idle').hidden, true);
  assert.equal(p.element('select').hidden, true);
  assert.equal(p.element('live').disabled, true);
  p.render({status: 'stopped', output: 'first', version: 3});
  assert.equal(p.element('idle').hidden, false);
  p.render({busy: true, status: 'starting', output: 'second', version: 4});
  assert.equal(p.element('idle').hidden, true);
  assert.equal(p.evaluate('leaving'), false);
  assert.equal(p.evaluate('watched'), true);
});

test('failed Stop leaves the active game visible and allows retry', async () => {
  const p = page();
  p.render({busy: true, status: 'running', output: 'first', version: 1});
  p.context.fetch = async () => { throw Error('connection lost'); };
  await p.evaluate('leave()');
  assert.equal(p.element('idle').hidden, true);
  assert.equal(p.evaluate('leaving'), false);
  assert.equal(p.evaluate('watched'), true);
  assert.match(p.element('error').textContent, /connection lost/);
});

test('start response opens the player without waiting for SSE and old updates cannot hide it', async () => {
  const p = page();
  p.context.fetch = async url => ({ok: true, json: async () => url === '/api/start' ?
    {state: {...p.context.state, busy: true, status: 'starting', output: 'new', version: 5}} : {ok: true}});
  assert.equal(await p.evaluate("startRun({mode: 'live', game: 'mario'})"), true);
  assert.equal(p.element('idle').hidden, true);
  p.render({status: 'stopped', output: 'old', version: 4});
  assert.equal(p.evaluate('view.output'), 'new');
  assert.equal(p.element('idle').hidden, true);
  p.evaluate('syncLobby(true)');
  assert.equal(p.element('idle').hidden, true, 'a lobby cannot cover a busy player');
});

test('stale leaving flag or active-run error cannot hide an existing player', async () => {
  const p = page();
  p.evaluate("leaving = 'old'");
  p.context.fetch = async () => ({ok: false, json: async () => ({error: 'A run is already active',
    state: {...p.context.state, busy: true, status: 'running', output: 'other', version: 8}})});
  assert.equal(await p.evaluate("startRun({mode: 'live'})"), false);
  assert.equal(p.element('idle').hidden, true);
  assert.equal(p.evaluate('leaving'), false);
});

test('visibility messages are scoped to the run and neither showing nor hiding starts a game', () => {
  const p = page();
  p.render({busy: true, status: 'running', output: 'first', version: 1});
  assert.equal(p.requests.at(-1).visible, true);
  p.context.document.hidden = true;
  p.listeners.visibilitychange();
  assert.equal(p.requests.at(-1).visible, false);
  assert.equal(p.requests.at(-1).departing, false, 'hiding is not closing');
  p.context.document.hidden = false;
  p.listeners.visibilitychange();
  assert.equal(p.requests.at(-1).visible, true);
  p.listeners.pagehide();
  assert.equal(p.requests.at(-1).visible, false);
  assert.equal(p.requests.at(-1).departing, true);
  assert.ok(p.requests.every(r => r.url === '/api/presence' && r.output === 'first'));
  assert.deepEqual(p.requests.map(r => r.sequence), [1, 2, 3, 4]);
  p.render({status: 'stopped', output: 'first', version: 2});
  p.intervals.forEach(fn => fn());
  assert.equal(p.requests.length, 4);
});

test('an idle lobby and hidden page cannot start or keep a live run alive', async () => {
  const p = page();
  p.render({});
  p.intervals.forEach(fn => fn());
  assert.equal(p.requests.length, 0);
  p.context.document.hidden = true;
  assert.equal(await p.evaluate("startRun({mode: 'live'})"), false);
  assert.equal(p.requests.length, 0);
});


test('a visibility pause keeps the same player and asks for explicit resume', () => {
  const p = page();
  p.render({busy: true, status: 'running', output: 'first', version: 1});
  p.render({busy: true, status: 'paused', paused: true, viewer_pause_reason: 'player_hidden', output: 'first', version: 2});
  assert.equal(p.element('idle').hidden, true);
  assert.equal(p.element('finale').hidden, true);
  assert.equal(p.evaluate('run.output'), 'first');
  assert.match(p.element('focus').textContent, /press play to continue/);
  assert.ok(p.requests.every(r => r.url === '/api/presence'));
});
