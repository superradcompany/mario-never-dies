'use strict';

// One screen: the game, one line of narration, the one timeline microsandbox made, and a
// dock beside it for a closer look (jev's decision, the futures of a fork, the calls).
// Beats are driven by controller events; the timeline is derived from them each update.
// Hover shows where you are; hold space and move to scrub; click to pin (select); drag
// the head (replay) to seek. Replays run on a game clock and tiles keep advancing between
// server updates from a preloaded frame cache. Anything you do responds in the same frame.

const $ = id => document.getElementById(id);
const CANDIDATES = ['right_run_jump', 'right_jump', 'jump', 'left'];
const LABEL = {noop: 'release', right: 'right', right_jump: 'right + jump', right_run: 'right + run', right_run_jump: 'right + run + jump', jump: 'jump', left: 'left', left_jump: 'left + jump', left_run: 'left + run', left_run_jump: 'left + run + jump', down: 'down', up: 'up'};
const SHORT = {noop: 'wait', right: 'right', right_jump: 'right + jump', right_run: 'run', right_run_jump: 'run + jump', jump: 'jump', left: 'left', left_jump: 'left + jump', left_run: 'left + run', left_run_jump: 'left + run + jump', down: 'down', up: 'up'};
Object.assign(LABEL, {flap: 'flap', glide: 'glide'});
Object.assign(SHORT, {flap: 'flap', glide: 'glide'});
const ENDED = new Set(['complete', 'failed', 'stopped', 'cleanup_failed']);
const SVG = 'http://www.w3.org/2000/svg';
const frameStep = () => view?.frame_step || 2;   // mario's worker keeps every second frame, flappy's every one
const LEAD_SECONDS = 0.14;      // how far a tile may run ahead of its last polled frame
const DEFAULT_HOLDS = {created: 0.4, death: 1.5, retry_hypothesis: 0.9, rewind: 1.0, rewind_refused: 0.9, multiverse: 1.0, promote: 1.3, race_failed: 0.9, stage_clear: 2.4, stage_started: 0.6, clear: 2.4};
const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const ms = value => typeof value === 'number' ? `${Math.round(value)} ms` : '';
// microsandbox's purple (--brand in the stylesheet), for what is drawn on a canvas: the copies it
// freezes, its branch times, its name. Lime stays the run's own colour.
const BRAND = '#c084fc';
const short = name => name ? name.split('-').at(-1) : '';
const clock = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const letter = index => typeof index === 'number' ? String.fromCharCode(97 + index) : '';

let view = null;
let run = null;            // per-run presentation state; reset when a new run starts
let busyRequest = false;
let watched = false;       // whether this page saw the current run while it was running
let markers = true;        // the markers knob
let skipDeaths = false;    // replay only: play just the verse that survived. never on by itself
let dockTab = 'moment';
let forkFocus = null;      // the fork (event.t) the futures tab is looking at

function freshRun(output) {
  return {
    output,
    serial: 0,
    lastEventT: 0,
    playedT: 0,            // the timeline and screen follow the beats, never the raw feed
    counts: {snapshots: 0, deaths: 0, rewinds: 0, forks: 0, verses: 0},
    ops: [],               // every microsandbox call so far, oldest first
    opsRendered: 0,
    beats: [],
    pumping: false,
    pumpEpoch: 0,
    lock: null,            // {mode, names} while a beat holds the screen
    winner: null,
    tiles: new Map(),
    traces: new Map(),     // timeline -> {samples: [[frame, x, elapsed]], at}
    graph: null,
    span: null,            // seconds the live timeline shows; grows in 1.5x steps
    stageT0: null,         // when the world on the timeline began; the line starts anew per world
    schedule: null,        // a replay's full schedule, for fitting each world to the line
    zoom: 1,               // how many screens wide the timeline is; the wheel widens it
    follow: true,          // keep the head in view while zoomed
    canon: null,           // the surviving path of a recording, once its schedule is known
    verses: new Map(),     // every verse of the run: its number and what differs in it
    versesFor: -1,
    hopTarget: null,       // the fold being jumped, so one jump is asked for once
    hopAt: 0,
    scan: null,            // {dir, ramp, t} while ⏪ or ⏩ runs the recorded frames
    ramped: false,         // the replay speed came from ⏩, so play resets it
    playRate: 1,           // how fast the recording plays from a moment behind live
    peekSeq: 0,
    geometry: null,        // what renderMap drew, for hit-testing
    peek: null,            // {name, frame, x, elapsed, slot, pinned, dead}
    hover: null,
    hot: null,             // timeline element lit from the dock
    focusTimer: null,
  };
}

// Replay beats hold for the same seconds the server clock does, scaled by playback speed.
function hold(kind) {
  const seconds = view?.holds?.[kind] ?? DEFAULT_HOLDS[kind] ?? 0;
  const speed = view?.mode === 'replay' ? (view.speed || 1) : 1;
  return sleep((seconds * 1000) / speed);
}

// ---------------------------------------------------------------- menus
//
// One dropdown for the whole page: a knob that opens a list beneath it. Built from
// markup, so the dock can render one inside its own HTML. A menu keeps its value in
// data-value and fires `menu-change` (bubbling) on the .menu element when it changes.

const CARET = '<svg class="caret" viewBox="0 0 7 4" width="9" height="6" shape-rendering="crispEdges" aria-hidden="true"><path d="M0 0h1v1h-1z M6 0h1v1h-1z M1 1h1v1h-1z M5 1h1v1h-1z M2 2h1v1h-1z M4 2h1v1h-1z M3 3h1v1h-1z"/></svg>';
function menuHtml(id, options, value, extra = '') {
  const current = options.find(option => String(option.value) === String(value)) || options[0];
  const items = options.map(option => `<button type="button" role="option" data-value="${esc(String(option.value))}" aria-selected="${option === current}">${esc(option.label)}</button>`).join('');
  return `<div class="menu ${extra}" id="${esc(id)}" data-value="${esc(String(current?.value ?? ''))}"><button type="button" class="menu-btn" aria-haspopup="listbox" aria-expanded="false"><span class="menu-label">${esc(current?.label ?? '')}</span>${CARET}</button><div class="menu-list" role="listbox" hidden>${items}</div></div>`;
}
// Rebuild a menu's options in place, keeping its element and classes.
function setMenu(root, options, value) {
  const template = document.createElement('template');
  template.innerHTML = menuHtml(root.id, options, value, [...root.classList].filter(name => !['menu', 'open'].includes(name)).join(' '));
  const fresh = template.content.firstElementChild;
  root.replaceChildren(...fresh.childNodes);
  root.dataset.value = fresh.dataset.value;
  root.classList.remove('open');
}
function setMenuValue(root, value) {
  const options = [...root.querySelectorAll('[role=option]')];
  const option = options.find(item => item.dataset.value === String(value));
  if (!option) return;
  root.dataset.value = String(value);
  root.querySelector('.menu-label').textContent = option.textContent;
  for (const item of options) item.setAttribute('aria-selected', String(item === option));
}
const menuValue = root => root?.dataset.value ?? '';
// A list opens under its knob (or over it, for an `up` menu) unless the window ends first:
// then it opens the other way, and it never grows past the room it has, so its last rows
// can always be reached. A list inside something that scrolls is left alone.
function placeMenu(menu) {
  const list = menu?.querySelector('.menu-list'), knob = menu?.querySelector('.menu-btn');
  if (!list || !knob || list.hidden) return;
  let clip = menu.parentElement;
  while (clip && clip !== document.body && getComputedStyle(clip).overflowY === 'visible') clip = clip.parentElement;
  if (clip && ['auto', 'scroll'].includes(getComputedStyle(clip).overflowY)) return;
  const bounds = clip && clip !== document.body ? rect(clip) : {top: 0, bottom: innerHeight};
  const box = rect(knob), gap = 14;
  const bar = document.querySelector('.bar');               // a list never climbs over the top bar
  const roomDown = Math.min(innerHeight, bounds.bottom) - box.bottom - gap, roomUp = box.top - Math.max(bar ? rect(bar).bottom : 0, bounds.top) - gap;
  menu.classList.remove('flip-up', 'flip-down');
  list.style.maxHeight = 'none';
  const want = Math.min(list.scrollHeight + 2, 560);
  const prefersUp = menu.classList.contains('up');
  const goesUp = prefersUp ? !(want > roomUp && roomDown > roomUp) : want > roomDown && roomUp > roomDown;
  menu.classList.toggle('flip-up', goesUp && !prefersUp);
  menu.classList.toggle('flip-down', !goesUp && prefersUp);
  list.style.maxHeight = `${Math.max(120, Math.min(want, goesUp ? roomUp : roomDown))}px`;
}
function closeMenus(except = null) {
  for (const menu of document.querySelectorAll('.menu.open')) {
    if (menu === except) continue;
    menu.classList.remove('open');
    menu.querySelector('.menu-list').hidden = true;
    menu.querySelector('.menu-btn').setAttribute('aria-expanded', 'false');
  }
}
document.addEventListener('click', event => {
  const knob = event.target.closest('.menu-btn');
  if (knob) {
    const menu = knob.closest('.menu');
    const open = !menu.classList.contains('open');
    closeMenus(menu);
    menu.classList.toggle('open', open);
    menu.querySelector('.menu-list').hidden = !open;
    knob.setAttribute('aria-expanded', String(open));
    if (open) { placeMenu(menu); (menu.querySelector('[role=option][aria-selected=true]') || menu.querySelector('[role=option]'))?.focus(); }
    else knob.blur();
    return;
  }
  const option = event.target.closest('.menu-list [role=option]');
  if (option) {
    const menu = option.closest('.menu');
    if (menu.classList.contains('actions')) {       // a menu of actions: nothing stays selected
      closeMenus();
      option.blur();
      menu.dispatchEvent(new CustomEvent('menu-change', {bubbles: true, detail: {value: option.dataset.value}}));
      return;
    }
    const before = menu.dataset.value;
    setMenuValue(menu, option.dataset.value);
    closeMenus();
    option.blur();
    if (before !== menu.dataset.value) menu.dispatchEvent(new CustomEvent('menu-change', {bubbles: true, detail: {value: menu.dataset.value}}));
    return;
  }
  if (!event.target.closest('.menu')) closeMenus();
});
// While a menu is open the keyboard belongs to it, not to the transport.
document.addEventListener('keydown', event => {
  const open = document.querySelector('.menu.open');
  if (!open) return;
  const options = [...open.querySelectorAll('[role=option]')];
  const index = options.indexOf(document.activeElement);
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeMenus(); }
  else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); event.stopPropagation(); options[(index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length]?.focus(); }
  else if ((event.key === ' ' || event.key === 'Enter') && index >= 0) { event.preventDefault(); event.stopPropagation(); options[index].click(); }
  else if (event.key === 'Tab') closeMenus();
}, true);
document.addEventListener('keyup', event => { if (event.key === ' ' && event.target.closest?.('.menu')) event.stopPropagation(); }, true);

// ---------------------------------------------------------------- server calls

async function command(path, body) {
  if (busyRequest) return false;
  busyRequest = true;
  $('error').hidden = true;
  try {
    const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'request failed');
    return true;
  } catch (error) {
    $('error').textContent = error.message;
    $('error').hidden = false;
    return false;
  } finally {
    busyRequest = false;
  }
}
const control = body => command('/api/control', body);

// ---------------------------------------------------------------- the lobby
//
// Attract mode: the best recording plays, dimmed, behind the title. A shelf along the
// bottom says what is showing, offers to watch or download it, and holds the other runs;
// "all" opens every recording as a card.

const ICON = {
  play: '<svg class="ic" viewBox="0 0 7 7" width="14" height="14" shape-rendering="crispEdges" aria-hidden="true"><path d="M2 0h1v1h-1z M2 1h1v1h-1z M2 2h1v1h-1z M2 3h1v1h-1z M2 4h1v1h-1z M2 5h1v1h-1z M2 6h1v1h-1z M3 1h1v1h-1z M3 2h1v1h-1z M3 3h1v1h-1z M3 4h1v1h-1z M3 5h1v1h-1z M4 2h1v1h-1z M4 3h1v1h-1z M4 4h1v1h-1z M5 3h1v1h-1z"/></svg>',
  download: '<svg class="ic" viewBox="0 0 7 7" width="14" height="14" shape-rendering="crispEdges" aria-hidden="true"><path d="M3 0h1v1h-1z M3 1h1v1h-1z M3 2h1v1h-1z M3 3h1v1h-1z M3 4h1v1h-1z M1 2h1v1h-1z M5 2h1v1h-1z M2 3h1v1h-1z M4 3h1v1h-1z M0 6h1v1h-1z M1 6h1v1h-1z M2 6h1v1h-1z M3 6h1v1h-1z M4 6h1v1h-1z M5 6h1v1h-1z M6 6h1v1h-1z"/></svg>',
};
// The last row of every download menu: the music the video carries. It opens the list of loops.
const SOUND_ROW = `<button type="button" role="option" data-value="sound" class="dl-sound" aria-expanded="false"><b class="dl-sound-label">music · ♪ auto</b><small>pick what the video plays</small>${CARET}</button>`;
const DOWNLOAD_OPTIONS = '<button type="button" role="option" data-value="video"><b>video · mp4</b><small>the run as you watched it, narration burned in</small></button><button type="button" role="option" data-value="cut"><b>video · the canon cut</b><small>only the verse that made it, as one unbroken run</small></button><button type="button" role="option" data-value="zip"><b>recording · zip</b><small>every frame, event and trace; replays on any machine</small></button>' + SOUND_ROW;
// The games. The controller, the timeline and the playback are shared; a game brings its
// name, its picture size and its words. `game` is the one on screen: the cabinet that was
// picked, or the game of the run that is playing.
const GAMES = {
  mario: {title: 'mario', hero: 'mario', heroes: 'marios', size: [256, 240], tall: false, step: 2, stage: '1-1',
    line: 'twelve moves, four worlds, a flag at the end.',
    machine: 'one machine: the emulator, the game, and jev on the controller.',
    died: 'mario died.', alive: 'mario is alive again', goal: stage => `world ${stage}`, flag: () => 'flag.', reached: 'reached a flag'},
  bird: {title: 'flappy', hero: 'flappy', heroes: 'birds', size: [288, 512], tall: true, step: 1, stage: '25', poster: '/assets/flappy-poster.png', soon: true,
    line: 'one button. one life. no end.',
    machine: 'one machine: the game, and jev on the one button.',
    died: 'the bird died.', alive: 'the bird is flying again', goal: stage => `${stage} pipes`, flag: stage => `${stage} pipes.`, reached: 'cleared the pipes'},
};
// A game marked soon keeps its cabinet on the menu but cannot be opened. ?preview=<game>
// opens it anyway, for whoever is still building it.
const previewing = new URLSearchParams(location.search).get('preview');
const gated = id => Boolean(GAMES[id]?.soon) && previewing !== id;
let game = 'mario';
let chosen = false;         // a cabinet was picked; until then the lobby is the game menu
const G = () => GAMES[game] || GAMES.mario;
const gameOf = item => (item && GAMES[item.game] ? item.game : 'mario');

let allRuns = [];           // every finished run, of every game, best first
let recordings = [];        // the ones of the game on screen
let featured = null;        // the recording on show behind the title
let libraryShort = false;   // short runs unfolded in the library

const runOutcome = item => item.status === 'complete' ? 'cleared' : item.completed_stages.length ? `cleared ${item.completed_stages.at(-1)}` : item.status === 'failed' ? 'failed' : 'stopped';
const runKind = item => item.status === 'complete' || item.completed_stages.length ? 'cleared' : item.status === 'failed' ? 'failed' : 'stopped';
// A recording's `rewinds` counts the deaths it recovered from; since every death forks, say so.
const runStats = item => `${plural(item.rewinds, 'death')} · ${plural(item.forks, 'fork')}`;
function runMeta(item) {
  const when = item.recorded ? new Date(item.recorded * 1000) : null;
  const date = when ? `${MONTHS[when.getMonth()]} ${when.getDate()} · ${String(when.getHours()).padStart(2, '0')}:${String(when.getMinutes()).padStart(2, '0')}` : 'recorded';
  return `${date} · ${clock(item.seconds)}`;
}
// A run that stopped before anything happened: no death, no fork, no stage.
const isShort = item => !item.rewinds && !item.forks && !item.completed_stages.length;
const rankRuns = (a, b) => (b.completed_stages.length - a.completed_stages.length) || ((b.status === 'complete') - (a.status === 'complete')) || (b.forks - a.forks) || (b.recorded - a.recorded);

async function loadRuns() {
  try {
    allRuns = [...await (await fetch('/api/runs')).json()].sort(rankRuns);
  } catch {
    allRuns = [];
  }
  recordings = allRuns.filter(item => gameOf(item) === game);
  if (!recordings.some(item => item.id === featured)) featured = recordings[0]?.id || null;
  renderCabinets();
  renderShelf();
  if (!$('library').hidden) renderLibrary();
  syncAttract();
}

// The game menu: one cabinet per game, each playing its best run. Picking one opens
// that game's lobby.
function renderCabinets() {
  for (const id of Object.keys(GAMES)) {
    const runs = allRuns.filter(item => gameOf(item) === id);
    const best = runs[0];
    const cabinet = document.querySelector(`.cabinet[data-game="${id}"]`);
    cabinet.classList.toggle('soon', gated(id));
    cabinet.setAttribute('aria-disabled', String(gated(id)));
    cabinet.setAttribute('aria-label', gated(id) ? `${GAMES[id].title}: coming soon` : `${GAMES[id].title}: open its lobby`);
    $(`cab-${id}-stats`).textContent = gated(id) ? 'soon' : runs.length ? `${plural(runs.length, 'recording')} · best: ${runOutcome(best)}` : 'no runs yet';
  }
}
function pickGame(id) {
  if (!GAMES[id] || gated(id)) return;
  game = id;
  chosen = true;
  featured = null;
  $('hint').textContent = '';
  applyGame();
  syncLobby(true);
  loadRuns();
}
function backToGames() {
  chosen = false;
  closeLibrary();
  syncLobby(true);
}
$('cabinets').addEventListener('click', event => { const cabinet = event.target.closest('[data-game]'); if (cabinet) pickGame(cabinet.dataset.game); });
$('to-games').onclick = () => backToGames();

// Everything on the page that depends on which game it is: the words, the shape of the
// picture, the controller under it.
function applyGame() {
  const running = Boolean(view && (view.busy || ['starting', 'running', 'paused'].includes(view.status) || (watched && ENDED.has(view.status))));
  document.body.dataset.game = game;
  document.body.classList.toggle('tall', G().tall && running && $('idle').hidden && $('select').hidden);
  for (const node of document.querySelectorAll('[data-hero]')) if (node.textContent !== G().hero) node.textContent = G().hero;
  const [w, h] = G().size;
  const peek = $('peek-canvas');
  if (peek.width !== w || peek.height !== h) { peek.width = w; peek.height = h; }
  document.title = document.body.classList.contains('picking') ? 'never dies' : `${G().hero} never dies`;
}

function renderShelf() {
  const item = recordings.find(entry => entry.id === featured);
  $('shelf').hidden = !item;
  $('shelf').classList.toggle('tall', G().tall);
  if (!item) { if (chosen && !$('hint').textContent) $('hint').textContent = 'no recordings yet. a live run records itself for replay.'; return; }
  $('shelf-title').textContent = `${runOutcome(item)} · ${runStats(item)}`;
  $('shelf-meta').textContent = runMeta(item);
  const strip = recordings.filter(entry => !isShort(entry) || entry.id === featured).slice(0, 5);
  replaceContent('shelf-strip', strip.map(entry => `<button type="button" class="strip-item${entry.id === featured ? ' on' : ''}" data-feature="${esc(entry.id)}" title="${esc(`${runOutcome(entry)} · ${runStats(entry)}`)}"><img src="/api/thumb/${esc(entry.id)}" alt=""><span>${esc(runOutcome(entry))}</span></button>`).join('')
    + `<button type="button" class="strip-all" id="strip-all">all ${recordings.length}</button>`);
}

function cardHtml(item, best) {
  const id = esc(item.id);
  return `<div class="rec-card"><button type="button" class="rec-thumb" data-watch="${id}" aria-label="replay this recording"><img src="/api/thumb/${id}" alt="" loading="lazy"><span class="rec-badge ${runKind(item)}">${esc(runOutcome(item))}</span></button>
    <div class="rec-row"><div class="rec-info"><span class="rec-stats">${esc(runStats(item))}${best ? '<span class="rec-best">best run</span>' : ''}</span><span class="rec-meta">${esc(runMeta(item))}</span></div>
    <div class="rec-actions"><button type="button" class="tb" data-watch="${id}" aria-label="replay this recording" title="replay">${ICON.play}</button><div class="menu actions right icon" data-run="${id}"><button type="button" class="menu-btn" aria-haspopup="listbox" aria-expanded="false" aria-label="download" title="download">${ICON.download}</button><div class="menu-list" role="listbox" hidden>${DOWNLOAD_OPTIONS}</div></div></div></div></div>`;
}

function renderLibrary() {
  const real = recordings.filter(item => !isShort(item)), short = recordings.filter(isShort);
  $('library-count').textContent = `${plural(recordings.length, 'recording')} of ${G().title}`;
  $('library-grid').classList.toggle('tall', G().tall);
  replaceContent('library-grid', (libraryShort ? [...real, ...short] : real).map((item, index) => cardHtml(item, index === 0 && !isShort(item))).join(''));
  $('library-short').hidden = !short.length;
  $('library-short').textContent = libraryShort ? `hide the ${plural(short.length, 'short run')}` : `${plural(short.length, 'short run')} hidden (stopped before the first death) · show`;
}
function openLibrary() { $('library').hidden = false; renderLibrary(); syncAttract(); }
function closeLibrary() { if ($('library').hidden) return; closeMenus(); $('library').hidden = true; syncAttract(); }
function watch(run) {
  if (!run) return;
  closeLibrary();
  command('/api/start', {mode: 'replay', run, speed: 1});
}

$('shelf-strip').addEventListener('click', event => {
  if (event.target.closest('#strip-all')) { openLibrary(); return; }
  const item = event.target.closest('[data-feature]');
  if (!item) return;
  featured = item.dataset.feature;
  renderShelf();
  syncAttract();
});
$('watch').onclick = () => watch(featured);
$('library-grid').addEventListener('click', event => { const target = event.target.closest('[data-watch]'); if (target) watch(target.dataset.watch); });
$('library-short').onclick = () => { libraryShort = !libraryShort; renderLibrary(); };
$('library-close').onclick = () => closeLibrary();
$('library').addEventListener('click', event => { if (event.target === $('library')) closeLibrary(); });
document.addEventListener('menu-change', event => {
  const menu = event.target;
  if (!menu.classList?.contains('actions')) return;
  download(menu.dataset.run || (menu.id === 'shelf-download' ? featured : recordingId()), event.detail.value);
});

// Attract loops: a recording on its game clock, drawn from the same frames a replay uses.
// One plays behind the lobby's title and one in each cabinet; each only runs while it is
// on screen. A game with no recording yet shows its poster.
function makeAttract(canvas, visible) {
  const context = canvas.getContext('2d');
  const loop = {run: null, game: null, schedule: null, started: 0, raf: 0, drawn: 0, painted: false};
  function size(id) {
    const [w, h] = GAMES[id].size;
    if (canvas.width !== w * 2 || canvas.height !== h * 2) { canvas.width = w * 2; canvas.height = h * 2; }
    context.imageSmoothingEnabled = false;
  }
  // A still to stand on: the game's poster when it has no recording, else the run's
  // thumbnail until its first frames arrive.
  function still(id, run) {
    size(id);
    context.fillStyle = '#000';
    context.fillRect(0, 0, canvas.width, canvas.height);
    const src = run ? `/api/thumb/${run}` : GAMES[id].poster;
    if (!src) return;
    const image = new Image();
    image.onload = () => { if (loop.run === run && loop.game === id && !loop.painted) context.drawImage(image, 0, 0, canvas.width, canvas.height); };
    image.src = src;
  }
  function tick(now) {
    loop.raf = 0;
    const schedule = loop.schedule;
    if (!schedule || !visible() || document.hidden) return;
    if (now - loop.drawn >= 33) {
      loop.drawn = now;
      const [w, h] = GAMES[loop.game].size;
      const t = ((now - loop.started) / 1000) % Math.max(1, schedule.end);
      const show = exportShow(schedule, t);
      show.names.forEach((name, index) => {
        const frame = exportFrame(schedule, name, t);
        if (frame === null) return;
        const image = frameCache.get(frameKey(name, frame));
        const [x, y, tw, th] = show.mode === 'grid' ? [(index % 2) * w, Math.floor(index / 2) * h, w, h] : [0, 0, w * 2, h * 2];
        if (image) { context.drawImage(image, x, y, tw, th); loop.painted = true; }
        for (let ahead = 0; ahead <= 8; ahead++) fetchFrame(name, frame + ahead * (schedule.frame_step || 2), loop.run);
      });
    }
    loop.raf = requestAnimationFrame(tick);
  }
  async function start(run, id) {
    if (loop.raf) cancelAnimationFrame(loop.raf);
    Object.assign(loop, {run, game: id, schedule: null, raf: 0, painted: false});
    still(id, run);
    if (!run) return;
    try {
      const response = await fetch(`/api/schedule?run=${run}`);
      if (!response.ok || loop.run !== run) return;
      loop.schedule = await response.json();
      loop.started = performance.now();
      sync(run, id);
    } catch {}
  }
  function sync(run, id) {
    const rest = () => { if (loop.raf) cancelAnimationFrame(loop.raf); loop.raf = 0; };
    if (!visible()) { rest(); return; }
    if (loop.run !== run || loop.game !== id) { start(run, id); return; }
    if (document.hidden) { rest(); return; }
    if (!loop.raf && loop.schedule) loop.raf = requestAnimationFrame(tick);
  }
  return {sync, loop, rest: () => still(loop.game, loop.run)};
}
const lobbyAttract = makeAttract($('attract'), () => !$('idle').hidden && $('library').hidden);
const cabinetAttract = Object.fromEntries(Object.keys(GAMES).map(id => [id, makeAttract($(`cab-${id}`), () => !$('select').hidden)]));
const attract = lobbyAttract.loop;   // what is showing behind the title

function syncAttract() {
  lobbyAttract.sync(featured, game);
  // A cabinet that is not open yet shows its poster, not a run.
  for (const [id, cabinet] of Object.entries(cabinetAttract)) cabinet.sync(gated(id) ? null : allRuns.find(item => gameOf(item) === id)?.id || null, id);
}
document.addEventListener('visibilitychange', () => syncAttract());

// Hovering a cabinet that is marked soon breaks its picture up like a bad signal: the poster
// through an ordered dither, cut into bands that each take their own colours, their edges
// out of register, strips torn sideways in other colours still, with the word on it.
function makeSoonGlitch(cabinet, canvas, id, rest) {
  const W = 144, H = 256;                                   // chunky: half the game's own pixels
  const BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];
  // The colours: the brand lime and its five siblings, the same three channel values in
  // every order, so the picture falls into many colours that still belong together.
  const INKS = [[213, 244, 92], [244, 92, 213], [92, 213, 244], [244, 213, 92], [213, 92, 244], [92, 244, 213]];
  const DARK = [14, 14, 18], PALE = [244, 246, 255];
  const css = ([r, g, b], alpha = 1) => `rgba(${r},${g},${b},${alpha})`;
  const sheet = () => { const made = document.createElement('canvas'); made.width = W; made.height = H; return made; };
  const small = sheet(), ghost = sheet();                   // the picture, and the same picture in other inks for the tears
  const pixels = small.getContext('2d', {willReadFrequently: true}), ghosts = ghost.getContext('2d');
  const out = pixels.createImageData(W, H), alt = ghosts.createImageData(W, H);
  const context = canvas.getContext('2d');
  const still = matchMedia('(prefers-reduced-motion: reduce)');
  const rand = n => Math.floor(Math.random() * n);
  let light = null, timer = 0, tick = 0, bands = [];
  const image = new Image();
  image.onload = () => {
    pixels.drawImage(image, 0, 0, W, H);
    const data = pixels.getImageData(0, 0, W, H).data;
    light = new Float32Array(W * H);
    for (let i = 0; i < W * H; i++) light[i] = (0.2126 * data[i * 4] + 0.7152 * data[i * 4 + 1] + 0.0722 * data[i * 4 + 2]) / 255;
  };
  image.src = GAMES[id].poster;

  // Cut the picture into bands. Each has its own inks, sits a little out of line, and
  // throws a coloured shadow of its bright parts to one side, like a plate out of register.
  function cut() {
    bands = [];
    let ink = rand(INKS.length);
    for (let y = 0; y < H; y += 12 + rand(56)) {
      ink = (ink + 1 + rand(INKS.length - 1)) % INKS.length;  // never the colour of the band above
      bands.push({y, ink, shift: rand(9) - 4, fringe: 2 + rand(4), pale: rand(9) === 0});
    }
  }
  const put = (data, at, [r, g, b]) => { data[at] = r; data[at + 1] = g; data[at + 2] = b; data[at + 3] = 255; };

  function draw() {
    if (!light) return;
    if (tick % 4 === 0) cut();                              // the colours hold for a few frames, then jump
    tick += 1;
    const boil = (Math.random() - 0.5) * 0.22;              // the threshold breathes, so the dots crawl
    let band = bands[0], next = 1;
    for (let y = 0; y < H; y++) {
      while (next < bands.length && bands[next].y <= y) band = bands[next++];
      const bright = band.pale ? PALE : INKS[band.ink], dim = INKS[(band.ink + 2) % 6];
      const shadow = INKS[(band.ink + 4) % 6], other = INKS[(band.ink + 3) % 6], otherDim = INKS[(band.ink + 5) % 6];
      for (let x = 0; x < W; x++) {
        const from = y * W + (x + band.shift + W) % W, at = (y * W + x) * 4;
        const value = light[from] * 1.25 + boil, lit = value > (BAYER[(y & 3) * 4 + (x & 3)] + 0.5) / 16, strong = value > 0.7;
        // Where the picture is dark, the bright parts a few pixels over show through in a third ink.
        const thrown = !strong && light[y * W + (x + band.shift - band.fringe + 2 * W) % W] * 1.25 > 0.7;
        put(out.data, at, strong && lit ? bright : thrown ? shadow : lit ? dim : DARK);
        put(alt.data, at, strong && lit ? other : lit ? otherDim : DARK);
      }
    }
    pixels.putImageData(out, 0, 0);
    ghosts.putImageData(alt, 0, 0);
    const {width, height} = canvas, scale = width / W;
    context.imageSmoothingEnabled = false;
    context.globalAlpha = 1;
    context.drawImage(small, 0, 0, width, height);
    for (let count = 3 + rand(4); count > 0; count--) {     // tears: strips slide sideways, most of them in the other inks
      const y = rand(H), h = 2 + rand(22), dx = (rand(2) ? 1 : -1) * (2 + rand(20));
      context.drawImage(rand(3) ? ghost : small, 0, y, W, h, dx * scale, y * scale, width, h * scale);
    }
    if (rand(6) === 0) {                                    // now and then a strip of colour bars, as on a test card
      const y = rand(height), h = Math.round(height * (0.02 + Math.random() * 0.05)), from = rand(INKS.length);
      INKS.forEach((_, index) => { context.fillStyle = css(INKS[(from + index) % 6]); context.fillRect(Math.round(index * width / 6), y, Math.ceil(width / 6), h); });
    }
    if (tick % 9 !== 0) {                                   // the word, split into its colours; it drops out now and then
      const size = Math.round(width * 0.24), y = Math.round(height * 0.5), split = 4 + rand(9);
      context.font = `500 ${size}px GeistPixelCircle, GeistPixelSquare, monospace`;
      context.textAlign = 'center';
      context.textBaseline = 'middle';
      context.fillStyle = 'rgba(14,14,18,.88)';
      context.fillRect(0, y - size * 0.7, width, size * 1.4);
      context.fillStyle = css(INKS[(tick >> 1) % 6], 0.9);
      context.fillText('soon', width / 2 - split, y);
      context.fillStyle = css(INKS[((tick >> 1) + 2) % 6], 0.9);
      context.fillText('soon', width / 2 + split, y + rand(5) - 2);
      context.fillStyle = css(PALE);
      context.fillText('soon', width / 2, y);
      context.textAlign = 'left';
      context.textBaseline = 'alphabetic';
      for (let count = 1 + rand(2); count > 0; count--) {   // and is torn with the picture under it
        const top = y - size * 0.7 + rand(Math.round(size * 1.2)), h = 6 + rand(Math.round(size * 0.35)), dx = (rand(2) ? 1 : -1) * (8 + rand(Math.round(width * 0.12)));
        context.drawImage(canvas, 0, top, width, h, dx, top, width, h);
      }
    }
    context.fillStyle = css(INKS[tick % 6], 0.3);           // the scan bar
    context.fillRect(0, (tick * 53) % height, width, Math.round(height * 0.012));
    for (let count = rand(5); count > 0; count--) {         // dropped blocks
      context.fillStyle = rand(4) ? css(INKS[rand(6)]) : css(DARK);
      context.fillRect(rand(width), rand(height), 8 + rand(64), 4 + rand(14));
    }
  }
  function start() {
    if (timer || !cabinet.classList.contains('soon')) return;
    draw();
    if (!still.matches) timer = setInterval(draw, 85);      // choppy on purpose
    else timer = -1;                                        // reduced motion: one broken frame, held
  }
  function stop() {
    if (!timer) return;
    if (timer > 0) clearInterval(timer);
    timer = 0;
    rest();
  }
  cabinet.addEventListener('pointerenter', start);
  cabinet.addEventListener('pointerleave', stop);
  cabinet.addEventListener('focus', start);
  cabinet.addEventListener('blur', stop);
  // No hover on a touch screen: a tap plays the effect for a moment.
  cabinet.addEventListener('click', () => { if (!cabinet.classList.contains('soon')) return; start(); setTimeout(() => { if (!cabinet.matches(':hover, :focus-visible')) stop(); }, 1100); });
  return {draw};
}
const soonGlitch = Object.fromEntries(Object.keys(GAMES).filter(id => GAMES[id].soon && GAMES[id].poster).map(id => [id, makeSoonGlitch(document.querySelector(`.cabinet[data-game="${id}"]`), $(`cab-${id}`), id, () => cabinetAttract[id].rest())]));

// render() decides whether the lobby shows; until a cabinet is picked, the game menu
// stands in for it.
let lobbyShown = null, lobbyWanted = true;
function syncLobby(wanted = lobbyWanted) {
  const lobby = lobbyWanted = Boolean(wanted);
  $('back').hidden = lobby;
  if (lobby) $('leave').hidden = true;
  $('select').hidden = !(lobby && !chosen);
  document.body.classList.toggle('picking', lobby && !chosen);
  $('idle').hidden = !(lobby && chosen);
  if (lobby !== lobbyShown) {
    lobbyShown = lobby;
    if (lobby) loadRuns(); else closeLibrary();
  }
  applyGame();
  syncAttract();
}

// Seeks never wait on each other: one request in flight, the newest target wins.
let seekPending = null, seekInFlight = false;
async function queueSeek(elapsed) {
  seekPending = elapsed;
  if (seekInFlight) return;
  seekInFlight = true;
  while (seekPending !== null) {
    const target = seekPending;
    seekPending = null;
    try { await fetch('/api/control', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action: 'seek', elapsed: target})}); } catch {}
  }
  seekInFlight = false;
}


$('live').onclick = () => command('/api/start', {mode: 'live', game, preview: previewing === game});
$('inter-download').querySelector('.menu-list').innerHTML = DOWNLOAD_OPTIONS;

// The world-clear overlay. It arrives a beat after the flag, so the moment itself is seen
// first; its last frame is where the prompts are. "look back" (or esc) puts it away and
// leaves the bar in the transport row, so the world can be scrubbed before moving on.
let clearCardTimer = null;
function scheduleClearCard(held) {
  clearTimeout(clearCardTimer);
  clearCardTimer = setTimeout(() => {
    clearCardTimer = null;
    if (!view?.intermission || !run) return;
    $('cc-stamp').textContent = `${G().goal(held.stage)} clear`;
    $('cc-stats').innerHTML = `<em>${plural(run.counts.deaths, 'death')}</em> · zero game overs`;
    $('cc-next').title = `${G().goal(held.next)} · enter`;
    $('stage').classList.add('cleared');
    $('clearcard').hidden = false;
    renderPlayback(true);
    setTimeout(() => { if (!$('clearcard').hidden) $('cc-next').focus({preventScroll: true}); }, 2900);
  }, view?.mode === 'replay' ? 600 : 2000);
}
function hideClearCard() {
  clearTimeout(clearCardTimer);
  clearCardTimer = null;
  if ($('clearcard').hidden && !$('stage').classList.contains('cleared')) return;
  $('clearcard').hidden = true;
  $('stage').classList.remove('cleared');
}
$('cc-download').querySelector('.menu-list').innerHTML = DOWNLOAD_OPTIONS;
$('cc-next').onclick = () => control({action: 'next'});
$('cc-back').onclick = () => { hideClearCard(); renderPlayback(true); };
$('clearcard').addEventListener('click', event => { if (event.target === $('clearcard') || event.target.classList.contains('cc-stamp') || event.target.classList.contains('cc-stats')) { hideClearCard(); renderPlayback(true); } });
$('inter-next').onclick = () => control({action: 'next'});
$('stop').onclick = () => command('/api/stop', {});
$('tp-pause').onclick = () => pressPlayPause();
$('tp-canon').onclick = () => {
  if (view?.mode !== 'replay' || !run?.canon) return;
  skipDeaths = !skipDeaths;
  run.hopTarget = null;
  $('map-scroll').scrollLeft = 0;
  canonHop();
  renderPlayback(true);
  renderMap();
  syncScreen();
};
$('tp-rew').onclick = () => pressRewind();
$('tp-ff').onclick = () => pressForward();
$('tp-back').onclick = () => pressSkip(-1);
$('tp-skip').onclick = () => pressSkip(1);
// ---------------------------------------------------------------- music
//
// The loops live in sound.js. This side only says where the run is: which world, whether
// four machines are racing, whether it is paused, rewinding, sped up or won.
let soundRunning = false;
function syncSound(running = soundRunning) {
  soundRunning = Boolean(running);
  if (!window.MarioSound) return;
  const won = view?.status === 'complete' && (soundRunning || !$('finale').hidden);
  if (won) { MarioSound.sync({running: true, won: true}); return; }
  if (!soundRunning || !view || !run) { MarioSound.sync({running: false}); return; }
  const replay = view.mode === 'replay';
  const shifted = run.scan && run.scan.dir > 0 && run.scan.ramp === 1;   // the recording playing at 1x
  MarioSound.sync({
    running: true,
    stage: view.stage,
    fork: currentShow().mode === 'grid',
    paused: Boolean(view.intermission) || (replay ? Boolean(view.paused) : Boolean(view.paused) && !run.peek?.pinned),
    scan: run.scan && !shifted ? run.scan.dir * run.scan.ramp : 0,
    speed: replay ? (view.speed || 1) : 1,
  });
}
function showSoundChoice() {
  const value = MarioSound.choice();
  setMenuValue($('tp-sound'), value);
  $('tp-sound').classList.toggle('muted', value === 'off');
}
if (window.MarioSound) {
  MarioSound.ready.then(tracks => {
    setMenu($('tp-sound'), [{value: 'auto', label: '♪ auto'}, ...tracks.map(item => ({value: item.id, label: `♪ ${item.title}`})), {value: 'off', label: '♪ off'}], MarioSound.choice());
    showSoundChoice();
  });
  $('tp-sound').addEventListener('menu-change', event => { MarioSound.choose(event.detail.value); showSoundChoice(); syncSound(); });
}

$('tp-speed').addEventListener('menu-change', event => {
  const speed = Number(event.detail.value);
  if (!view || !run || !speed) return;
  if (view.mode === 'replay') { run.ramped = false; control({action: 'speed', speed}); return; }
  run.playRate = speed;
  if (run.scan?.dir > 0) { run.scan.ramp = speed; pop(speed === 1 ? 'play' : 'forward', speed === 1 ? '' : `${speed}×`); }
  renderPlayback(true);
});
$('frame').addEventListener('click', () => pressPlayPause());
$('tp-markers').onclick = () => { markers = !markers; $('tp-markers').classList.toggle('on', markers); $('tp-markers').setAttribute('aria-pressed', String(markers)); renderMap(); };
// Back to the lobby. A live run is the only thing that is lost by leaving, so only it asks.
let leaving = false;
async function leave() {
  $('leave').hidden = true;
  leaving = true;
  await command('/api/stop', {});
  $('finale').hidden = true;
  watched = false;
  syncLobby(true);
}
$('back').onclick = () => {
  const live = view?.mode !== 'replay' && (view?.busy || ['starting', 'running', 'paused'].includes(view?.status));
  if (live) { $('leave').hidden = !$('leave').hidden; if (!$('leave').hidden) $('leave-stay').focus(); }
  else leave();
};
$('leave-stay').onclick = () => { $('leave').hidden = true; };
$('leave-go').onclick = () => leave();
document.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('leave').hidden) $('leave').hidden = true; });
document.addEventListener('click', event => { if (!$('leave').hidden && !event.target.closest('.brand')) $('leave').hidden = true; });

$('again').onclick = async () => { await command('/api/stop', {}); $('finale').hidden = true; watched = false; syncLobby(true); };
$('finale-poster').onclick = () => saveMultiversePoster();
$('multiverse-save').onclick = () => saveMultiversePoster();
$('scrub').onclick = () => { $('finale').hidden = true; };
$('peek-close').onclick = () => clearPeek();
$('peek-seek').onclick = async () => {
  if (!run?.peek) return;
  const elapsed = run.peek.elapsed;
  clearPeek();
  if (await control({action: 'seek', elapsed})) await control({action: 'play'});
};
$('peek-rewind').onclick = async () => {
  if (!run?.peek?.slot) return;
  const slot = run.peek.slot;
  clearPeek();
  await control({action: 'rewind', slot});
};
// A clicked knob must not keep the keyboard: space stays global.
for (const button of document.querySelectorAll('.kb, .tb, .pin button, .dock-tabs button')) button.addEventListener('click', () => button.blur());

// Space: tap = pause / play; hold and move over the timeline = scrub, no toggle on release.
let spaceHeld = false, spaceMoved = false;
let hintDone = false;
try { hintDone = localStorage.getItem('mnd.scrubbed') === '1'; } catch {}
function hintUsed() {
  hintDone = true;
  $('scrub-hint').hidden = true;
  try { localStorage.setItem('mnd.scrubbed', '1'); } catch {}
}
document.addEventListener('keydown', event => {
  if (['SELECT', 'INPUT', 'TEXTAREA'].includes(event.target.tagName)) return;
  if (event.key === 'Escape' && !$('library').hidden) { closeLibrary(); return; }
  if (event.key === 'Escape' && !$('clearcard').hidden) { hideClearCard(); renderPlayback(true); return; }
  if (event.key === 'Escape') { if (run?.scan) endScan(view?.mode === 'replay' ? 'stop' : 'live'); else clearPeek(); return; }
  if (event.key === 'Enter' && view?.intermission && !exporting && !event.target.closest('button, .menu')) { event.preventDefault(); control({action: 'next'}); return; }
  if (event.key === 'm' && !event.metaKey && !event.ctrlKey && !event.altKey && window.MarioSound) { MarioSound.toggle(); showSoundChoice(); syncSound(); return; }
  if (event.key === 'r' && !event.metaKey && !event.ctrlKey && !event.altKey) { event.preventDefault(); if (recording) stopRecording(); else startRecording(); return; }
  if (event.key === 'f' && !event.metaKey && !event.ctrlKey && !event.altKey && !$('tp-fullscreen').hidden) { event.preventDefault(); toggleFullscreen(); return; }
  if (event.key === ' ') {
    event.preventDefault();
    if (event.repeat) return;
    // Holding space arms scrubbing; only moving the pointer starts it. A tap stays a tap.
    spaceHeld = true;
    spaceMoved = false;
    updateCursor();
    return;
  }
  if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && !event.target.closest('[role=tab]')) {
    event.preventDefault();
    if (event.repeat) return;
    const dir = event.key === 'ArrowLeft' ? -1 : 1;
    if (event.shiftKey) pressSkip(dir); else if (dir < 0) pressRewind(); else pressForward();
    return;
  }
  if ((event.key === ',' || event.key === '.') && run?.peek?.pinned) {
    event.preventDefault();
    stepPeek(event.key === ',' ? -1 : 1);
  }
});
document.addEventListener('keyup', event => {
  if (event.key !== ' ' || !spaceHeld) return;
  event.preventDefault();   // a focused button must not fire its own click on the same tap
  spaceHeld = false;
  updateCursor();
  if (spaceMoved) { if (!run?.peek?.pinned) clearPeek(); }
  else pressPlayPause();
});
window.addEventListener('blur', () => { spaceHeld = false; updateCursor(); });

// ---------------------------------------------------------------- transport
//
// One bar, both modes. ⏯ pauses whatever is moving and plays from wherever the view is.
// ⏪ runs the recorded frames backwards, faster each press (2× 4× 8×). ⏩ ramps a replay's
// speed the same way; in a live run it can only fast-forward a view that is behind, and
// it is off at the live point. Live, the machines keep running while you look back.

const RAMPS = [2, 4, 8];
const SPEEDS = [0.5, 1, 2, 4, 8];
const SKIP = 10;   // seconds a skip moves
const GLYPH = {
  skipback: 'M0 1h1v1h-1z M0 2h1v1h-1z M0 3h1v1h-1z M0 4h1v1h-1z M0 5h1v1h-1z M2 3h1v1h-1z M3 2h1v1h-1z M3 3h1v1h-1z M3 4h1v1h-1z M4 1h1v1h-1z M4 2h1v1h-1z M4 3h1v1h-1z M4 4h1v1h-1z M4 5h1v1h-1z M5 0h1v1h-1z M5 1h1v1h-1z M5 2h1v1h-1z M5 3h1v1h-1z M5 4h1v1h-1z M5 5h1v1h-1z M5 6h1v1h-1z',
  skipfwd: 'M1 0h1v1h-1z M1 1h1v1h-1z M1 2h1v1h-1z M1 3h1v1h-1z M1 4h1v1h-1z M1 5h1v1h-1z M1 6h1v1h-1z M2 1h1v1h-1z M2 2h1v1h-1z M2 3h1v1h-1z M2 4h1v1h-1z M2 5h1v1h-1z M3 2h1v1h-1z M3 3h1v1h-1z M3 4h1v1h-1z M4 3h1v1h-1z M6 1h1v1h-1z M6 2h1v1h-1z M6 3h1v1h-1z M6 4h1v1h-1z M6 5h1v1h-1z',
  play: 'M2 0h1v1h-1z M2 1h1v1h-1z M2 2h1v1h-1z M2 3h1v1h-1z M2 4h1v1h-1z M2 5h1v1h-1z M2 6h1v1h-1z M3 1h1v1h-1z M3 2h1v1h-1z M3 3h1v1h-1z M3 4h1v1h-1z M3 5h1v1h-1z M4 2h1v1h-1z M4 3h1v1h-1z M4 4h1v1h-1z M5 3h1v1h-1z',
  pause: 'M1 0h1v1h-1z M1 1h1v1h-1z M1 2h1v1h-1z M1 3h1v1h-1z M1 4h1v1h-1z M1 5h1v1h-1z M1 6h1v1h-1z M2 0h1v1h-1z M2 1h1v1h-1z M2 2h1v1h-1z M2 3h1v1h-1z M2 4h1v1h-1z M2 5h1v1h-1z M2 6h1v1h-1z M4 0h1v1h-1z M4 1h1v1h-1z M4 2h1v1h-1z M4 3h1v1h-1z M4 4h1v1h-1z M4 5h1v1h-1z M4 6h1v1h-1z M5 0h1v1h-1z M5 1h1v1h-1z M5 2h1v1h-1z M5 3h1v1h-1z M5 4h1v1h-1z M5 5h1v1h-1z M5 6h1v1h-1z',
  rewind: 'M0 3h1v1h-1z M1 2h1v1h-1z M1 3h1v1h-1z M1 4h1v1h-1z M2 1h1v1h-1z M2 2h1v1h-1z M2 3h1v1h-1z M2 4h1v1h-1z M2 5h1v1h-1z M4 3h1v1h-1z M5 2h1v1h-1z M5 3h1v1h-1z M5 4h1v1h-1z M6 1h1v1h-1z M6 2h1v1h-1z M6 3h1v1h-1z M6 4h1v1h-1z M6 5h1v1h-1z',
  forward: 'M0 1h1v1h-1z M0 2h1v1h-1z M0 3h1v1h-1z M0 4h1v1h-1z M0 5h1v1h-1z M1 2h1v1h-1z M1 3h1v1h-1z M1 4h1v1h-1z M2 3h1v1h-1z M4 1h1v1h-1z M4 2h1v1h-1z M4 3h1v1h-1z M4 4h1v1h-1z M4 5h1v1h-1z M5 2h1v1h-1z M5 3h1v1h-1z M5 4h1v1h-1z M6 3h1v1h-1z',
};
const speedLabel = speed => speed === 0.5 ? '½×' : `${speed}×`;

// Where the view is: a replay's position, or the moment a live run is looking at.
function viewT() {
  if (run?.scan) return run.scan.t;
  if (view?.mode !== 'replay' && run?.peek?.pinned && typeof run.peek.elapsed === 'number') return run.peek.elapsed;
  return view?.elapsed || 0;
}
const atEnd = () => view?.mode === 'replay' && !run?.scan && Boolean(view.duration) && view.elapsed >= view.duration - 0.05;
function moving() {
  if (run?.scan) return true;
  if (view?.mode === 'replay') return !view.paused;
  if (run?.peek?.pinned) return false;
  return !view?.paused;
}

// Which machine was the present at a moment, from what the timeline drew.
function presentAt(t) {
  const main = (run?.geometry?.lanes || []).filter(lane => lane.kind === 'main');
  const hit = main.find(lane => t >= lane.t0 - 0.3 && t <= lane.t1 + 0.3) || main.filter(lane => lane.t0 <= t).at(-1) || main.at(-1);
  return hit?.name || view?.trunk || null;
}

async function pressPlayPause() {
  if (!view?.busy || !run) return;
  const replay = view.mode === 'replay';
  if (run.scan) { pop('pause'); endScan(replay ? 'stop' : 'park'); return; }
  if (replay) {
    if (run.drag) return;
    if (view.paused) {
      pop('play');
      if ($('focus').textContent.startsWith('paused here')) setFocus('rolling.');
      if (run.ramped) { run.ramped = false; await control({action: 'speed', speed: 1}); }
      await control({action: 'play'});
    } else { pop('pause'); setFocus('paused here. press space to play.'); await control({action: 'pause'}); }
    return;
  }
  if (run.peek?.pinned && typeof run.peek.elapsed === 'number') { startScan(1, run.playRate || 1, run.peek.elapsed); return; }
  pop(view.paused ? 'play' : 'pause');
  await control({action: view.paused ? 'play' : 'pause'});
}

function pressRewind() {
  if (!view?.busy || !run) return;
  if (run.scan?.dir < 0) { rampScan(); return; }
  if (run.scan) { run.scan.dir = -1; run.scan.ramp = 2; run.scan.at = performance.now(); pop('rewind', '2×'); renderPlayback(true); return; }
  const t = viewT();
  if (t <= (run.geometry?.t0 || 0) + 0.05) return;
  if (view.mode === 'replay' && !view.paused) control({action: 'pause'});
  startScan(-1, 2, t);
}

async function pressForward() {
  if (!view?.busy || !run) return;
  const replay = view.mode === 'replay';
  if (run.scan?.dir > 0) { rampScan(); return; }
  if (run.scan) {
    if (replay) { endScan('stop', {play: true, speed: 2}); return; }
    run.scan.dir = 1; run.scan.ramp = 2; run.scan.at = performance.now(); pop('forward', '2×'); renderPlayback(true);
    return;
  }
  if (replay) {
    if (atEnd() || run.drag) return;
    const next = RAMPS.find(ramp => ramp > (view.speed || 1)) || 1;
    run.ramped = next !== 1;
    pop('forward', speedLabel(next));
    await control({action: 'speed', speed: next});
    if (view.paused) await control({action: 'play'});
    return;
  }
  if (run.peek?.pinned && typeof run.peek.elapsed === 'number') startScan(1, 2, run.peek.elapsed);
}

// A skip: a fixed hop back or ahead. Where there is less than that, it lands on the oldest
// point of this world or on the latest one (live, or the end of a replay).
async function pressSkip(dir) {
  if (!view?.busy || !run) return;
  const replay = view.mode === 'replay';
  const t0 = run.geometry?.t0 || 0;
  const latest = replay ? (view.duration || Infinity) : (view.elapsed || 0);
  const kind = dir < 0 ? 'skipback' : 'skipfwd';
  if (run.scan) {                                   // a running scan just jumps
    run.scan.t = Math.max(t0, Math.min(latest, run.scan.t + dir * SKIP));
    run.scan.lastPeek = run.scan.lastMap = run.scan.lastSeek = 0;
    pop(kind, `${SKIP} s`);
    return;
  }
  if (replay) {
    if (run.drag) return;
    const here = view.elapsed || 0;
    const hop = canonOn() ? run.canon.toFull(run.canon.toCanon(here) + dir * SKIP) : here + dir * SKIP;
    const target = Math.max(t0, Math.min(latest, hop));
    if (Math.abs(target - here) < 0.05) return;
    pop(kind, `${SKIP} s`);
    queueSeek(target);
    return;
  }
  const target = viewT() + dir * SKIP;
  if (dir > 0 && target >= latest - 0.05) { pop(kind, ''); clearPeek(); renderPlayback(true); return; }
  const t = Math.max(t0, target);
  pop(kind, `${SKIP} s`);
  const name = presentAt(t);
  if (name) await peekAt({name, t, dead: false, kind: 'lane', y: run.geometry?.presentY ?? 40}, true);
  renderPlayback(true);
}

function startScan(dir, ramp, t) {
  endScanQuiet();
  run.scan = {dir, ramp, t, at: performance.now(), lastSeek: 0, lastPeek: 0, lastMap: 0};
  if (view.mode === 'replay') { run.drag = {elapsed: t, released: false}; run.dragT = t; }
  if (run.peek?.pinned) { run.peek = null; renderPeek(); renderMap(); }
  pop(dir < 0 ? 'rewind' : ramp === 1 ? 'play' : 'forward', ramp === 1 ? '' : `${ramp}×`);
  renderPlayback(true);
  scheduleScan();
}

function rampScan() {
  const scan = run.scan;
  scan.ramp = RAMPS[(RAMPS.indexOf(scan.ramp) + 1) % RAMPS.length];
  pop(scan.dir < 0 ? 'rewind' : 'forward', `${scan.ramp}×`);
  renderPlayback(true);
}

function scanTick(now) {
  const scan = run?.scan;
  if (!scan || !view) return;
  // Real time, so the ramp is honest at any frame rate; a long stall (hidden tab) just pauses it.
  const dt = Math.min(1, Math.max(0, (now - scan.at) / 1000));
  scan.at = now;
  if (canonOn()) scan.t = run.canon.toFull(run.canon.toCanon(scan.t) + scan.dir * scan.ramp * dt);
  else scan.t += scan.dir * scan.ramp * dt;
  const replay = view.mode === 'replay';
  const ceiling = replay ? (view.duration || Infinity) : (view.elapsed || 0);
  const floor = run.geometry?.t0 || 0;
  if (scan.t <= floor) { scan.t = floor; endScan(replay ? 'stop' : 'park'); return; }
  if (scan.dir > 0 && scan.t >= ceiling) { scan.t = ceiling; endScan(replay ? 'stop' : 'live'); return; }
  showScan(now);
  scheduleScan();
}

function scheduleScan() {
  if (document.hidden) setTimeout(() => scanTick(performance.now()), 100);
  else requestAnimationFrame(scanTick);
}

function showScan(now) {
  const scan = run.scan;
  const name = presentAt(scan.t);
  const y = run.geometry?.presentY ?? 40;
  run.hover = {kind: 'present', t: scan.t, y, name, dead: false};
  renderCursor();
  if (view.mode === 'replay') {
    run.drag = run.drag || {elapsed: scan.t, released: false};
    run.drag.elapsed = scan.t;
    run.dragT = scan.t;
    if (now - scan.lastSeek > 400) { scan.lastSeek = now; queueSeek(scan.t); }
    if (now - scan.lastMap > 80) { scan.lastMap = now; renderMap(); renderClock(); }
  }
  if (name && now - scan.lastPeek > 50) {
    scan.lastPeek = now;
    peekAt({name, t: scan.t, dead: false, kind: 'lane', y}, false, () => run.scan === scan);
  }
}

// The position readout: where the playback is (a replay), or how long the run has been going.
function renderClock() {
  if (!view || !run) return;
  const replay = view.mode === 'replay';
  const position = run.scan && replay ? run.scan.t : run.drag ? run.drag.elapsed : view.elapsed || 0;
  // A fixed width per mode, so the line's end never shifts as the digits change.
  $('tp-clock').classList.toggle('short', !(replay && view.duration));
  if (canonOn()) $('tp-clock').textContent = `${clock(run.canon.toCanon(position))} / ${clock(run.canon.total)}`;
  else $('tp-clock').textContent = replay && view.duration ? `${clock(position)} / ${clock(view.duration)}` : clock(position);
}

function scanCaption() {
  const scan = run?.scan;
  if (!scan) return '';
  if (scan.dir < 0) return `rewinding · ${scan.ramp}×`;
  return scan.ramp === 1 ? 'playing the recording from here.' : `fast forward · ${scan.ramp}×`;
}

// Drop a scan without landing anywhere: something else is taking the wheel.
function endScanQuiet() {
  const scan = run?.scan;
  if (!scan) return;
  run.scan = null;
  run.hover = null;
  renderCursor();
  if (view?.mode === 'replay' && run.drag) { run.drag.released = true; queueSeek(scan.t); }
}

function seeksSettled() {
  return new Promise(resolve => { const tick = () => (seekInFlight || seekPending !== null) ? setTimeout(tick, 20) : resolve(); tick(); });
}

// Land the scan: a replay stops (or plays on) from there, a live view parks on that moment
// or goes back to live.
async function endScan(reason, options = {}) {
  const scan = run?.scan;
  if (!scan) return;
  const owner = run;
  run.scan = null;
  run.hover = null;
  renderCursor();
  const t = scan.t;
  if (view.mode === 'replay') {
    run.drag = run.drag || {elapsed: t, released: false};
    run.drag.elapsed = t;
    run.drag.released = true;
    run.dragT = t;
    queueSeek(t);
    await seeksSettled();
    if (run !== owner) return;
    if (options.play) {
      if (options.speed) { run.ramped = options.speed !== 1; await control({action: 'speed', speed: options.speed}); }
      await control({action: 'play'});
    }
  } else if (reason === 'live') {
    clearPeek();
  } else {
    const name = presentAt(t);
    if (name) await peekAt({name, t, dead: false, kind: 'lane', y: run.geometry?.presentY ?? 40}, true);
  }
  if (run === owner) renderPlayback(true);
}

// The glyph that pops on the game for every transport press.
let popTimer = null;
function pop(kind, text = '') {
  const box = $('pop');
  $('pop-glyph').innerHTML = `<path d="${GLYPH[kind]}"></path>`;
  $('pop-text').textContent = text;
  box.hidden = false;
  box.classList.remove('go');
  void box.offsetWidth;
  box.classList.add('go');
  clearTimeout(popTimer);
  popTimer = setTimeout(() => { box.hidden = true; }, 520);
}

// ---------------------------------------------------------------- narration

function setFocus(html) {
  const node = $('focus');
  if (node.innerHTML === html) return;
  clearTimeout(run?.focusTimer);
  node.classList.add('out');
  const owner = run, epoch = run?.pumpEpoch;
  clearTimeout(run?.focusSetTimer);
  run.focusSetTimer = setTimeout(() => {
    if (run !== owner || run.pumpEpoch !== epoch) return;
    node.innerHTML = html; node.classList.remove('out');
  }, 180);
}

function later(delay, html) {
  clearTimeout(run.focusTimer);
  const owner = run, epoch = run.pumpEpoch;
  run.focusTimer = setTimeout(() => { if (run === owner && run.pumpEpoch === epoch) setFocus(html); }, delay);
}

// The SDK calls each controller event stands for. Each carries the event it came from so
// the calls list can point back at the timeline.
function opsFor(event) {
  const at = event.elapsed;
  const entry = (call, detail) => ({call, detail, at, event});
  switch (event.type) {
    case 'created': return [entry('Sandbox.create()', 'mario:local')];
    case 'checkpoint': return [entry('sandbox.branch()', ms(event.branch_ms)), entry('child.pause()', 'frozen')];
    case 'death': return [entry('sandbox.kill()', 'dead future')];
    case 'rewind': return event.manual ? [entry('sandbox.kill()', 'your call'), entry('checkpoint.branch()', ms(event.branch_ms))] : [entry('checkpoint.branch()', ms(event.branch_ms))];
    case 'multiverse': return [entry(`checkpoint.branch_many(${event.children?.length || 4})`, `${event.children?.length || 4} machines`)];
    case 'promote': return [entry('sandbox.kill()', '× 3 losers')];
    case 'stage_started': return [entry('Sandbox.create()', G().goal(event.stage || ''))];
    case 'paused': return [entry('sandbox.pause()', `× ${event.machines?.length || 1} frozen mid-frame`)];
    case 'intermission': return [entry('sandbox.pause()', 'frozen at the flag')];
    case 'resumed': return [entry('sandbox.resume()', `× ${event.machines?.length || 1}`)];
    default: return [];
  }
}

function pushOps(entries) {
  run.ops.push(...entries);
  renderOps();
}

function renderOps() {
  const list = $('ops');
  const panel = $('dock-calls');
  const follow = panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 12;
  if (run.opsRendered > run.ops.length || run.opsRendered === 0) { list.replaceChildren(); run.opsRendered = 0; }
  for (let index = run.opsRendered; index < run.ops.length; index++) {
    const item = run.ops[index];
    const row = document.createElement('li');
    row.dataset.index = String(index);
    row.innerHTML = `<code>${esc(item.call)}</code><span${/\d ms$/.test(item.detail || '') ? ' class="ms"' : ''}>${esc(item.detail || '')}</span><em>${typeof item.at === 'number' ? clock(item.at) : ''}</em>`;
    list.append(row);
  }
  run.opsRendered = run.ops.length;
  if (follow) panel.scrollTop = panel.scrollHeight;
}

// Hovering a call lights its point on the timeline; clicking selects that moment.
// Hovering a call lights its point on the timeline; clicking selects that moment.
function opTarget(item) {
  const event = item.event;
  if (!event || !run.geometry) return null;
  const node = slot => run.geometry.nodes.find(n => n.slot === slot);
  switch (event.type) {
    case 'checkpoint': { const n = node(event.child); return n ? {...n, kind: 'node'} : null; }
    case 'rewind': case 'multiverse': { const n = node(event.parent); return n ? {...n, kind: 'node'} : null; }
    case 'death': return {name: event.sandbox, t: event.elapsed, dead: true, kind: 'lane', y: laneYOf(event.sandbox, event.elapsed)};
    case 'promote': return {name: event.sandbox, t: event.elapsed, dead: false, kind: 'lane', y: laneYOf(event.sandbox, event.elapsed)};
    default: return null;
  }
}

function laneYOf(name, t) {
  const lanes = run.geometry?.lanes || [];
  const lane = lanes.find(item => item.name === name && (t === undefined || (t >= item.t0 - 0.5 && t <= item.t1 + 0.5))) || lanes.find(item => item.name === name);
  return lane ? lane.y : run.geometry?.presentY;
}

$('ops').addEventListener('mouseover', event => {
  const row = event.target.closest('li');
  if (!row || !run) return;
  const item = run.ops[Number(row.dataset.index)];
  const target = item && opTarget(item);
  run.hot = target ? (target.slot ? {slot: target.slot} : {name: target.name}) : null;
  renderHot();
});
$('ops').addEventListener('mouseleave', () => { if (run) { run.hot = null; renderHot(); } });
$('ops').addEventListener('click', event => {
  const row = event.target.closest('li');
  if (!row || !run) return;
  const item = run.ops[Number(row.dataset.index)];
  const target = item && opTarget(item);
  if (target) { run.peek = null; peekAt(target, true); }
});
function renderHot() {
  for (const node of document.querySelectorAll('#map .hot')) node.classList.remove('hot');
  const hot = run?.hot;
  if (!hot) return;
  if (hot.slot) document.getElementById(`group-${hot.slot}`)?.classList.add('hot');
  else for (const node of document.querySelectorAll(`#map [data-name="${CSS.escape(hot.name)}"]`)) node.classList.add('hot');
}

function bump(key) {
  const node = $(`c-${key}`)?.parentElement;
  if (!node) return;                       // a count the header no longer shows (rewinds)
  node.classList.remove('bump');
  void node.offsetWidth;
  node.classList.add('bump');
}

function stamp(text) {
  const node = $('stamp');
  node.hidden = true;
  node.textContent = text;
  void node.offsetWidth;
  node.hidden = false;
  setTimeout(() => { node.hidden = true; }, 2200);
}

function tally(event) {
  if (event.type === 'checkpoint') run.counts.snapshots += 1;
  if (event.type === 'death') run.counts.deaths += 1;
  if (event.type === 'rewind') run.counts.rewinds += 1;
  if (event.type === 'multiverse') { run.counts.forks += 1; run.counts.verses += (event.children || []).length; }
}

// ---------------------------------------------------------------- verses
//
// Every machine a split creates is a verse. Each gets a number for the whole run, and a
// few words for what is different in it: the opening it is forced through before jev takes over.

const TRAIT_APPROACH = {noop: 'waits, then jumps', left: 'backs up first', right: 'walks in, then jumps', right_run: 'runs in, then jumps'};
// The one-button game: a verse differs in which part of the gap it steers for (and it
// flaps on another beat than the bird that died).
const aimTrait = aim => aim < 0.35 ? 'aims high' : aim < 0.5 ? 'aims a little high' : aim <= 0.62 ? 'aims for the middle' : aim < 0.78 ? 'aims a little low' : 'aims low';
function traitOf(plan) {
  const steps = plan?.steps || [];
  if (typeof plan?.aim === 'number') return aimTrait(plan.aim);
  if (!steps.length) return '';
  const at = steps.findIndex(step => String(step.action).includes('jump'));
  const jump = steps[Math.max(0, at)];
  const approach = at > 0 ? steps[0] : null;
  if (approach && TRAIT_APPROACH[approach.action]) return TRAIT_APPROACH[approach.action];
  const running = String(jump.action).includes('run');
  if (jump.frames >= 32) return 'holds the jump longer';
  if (jump.frames <= 16) return running ? 'short running hop' : 'short hop';
  return running ? 'running jump' : 'jumps without running';
}
function verseModel(events) {
  const verses = new Map();
  let count = 0;
  for (const event of events) {
    if (event.type !== 'multiverse') continue;
    const taken = new Map();
    (event.children || []).forEach((name, index) => {
      const plan = event.experiments?.[name] || null;
      let trait = traitOf(plan);
      if (trait) {
        const again = taken.get(trait) || 0;
        taken.set(trait, again + 1);
        // two verses with the same idea differ in how long they hold it
        if (again) trait = `${trait} · ${(plan.steps.find(step => !String(step.action).includes('jump')) || plan.steps[0]).frames}f`;
      }
      verses.set(name, {no: ++count, trait, index, fork: event.t});
    });
  }
  return verses;
}
function verseOf(name) {
  if (!run || !view) return null;
  if (run.versesFor !== view.events.length) { run.verses = verseModel(view.events); run.versesFor = view.events.length; }
  return run.verses.get(name) || null;
}
const verseName = name => { const verse = verseOf(name); return verse ? `verse ${verse.no}` : 'verse'; };
const verseTitle = name => { const verse = verseOf(name); return verse ? `verse ${verse.no}${verse.trait ? ` · ${verse.trait}` : ''}` : 'verse'; };

// ---------------------------------------------------------------- the map of the multiverse
//
// Every verse the run created, as a branch. The lime line is canon: the surviving path, on
// the cut's own clock, so it runs unbroken from left to right. Wherever a loop was folded
// away, everything that collapsed there fans out: the present that died, and each verse of
// every split, except the one that carried on.

function multiverseModel(events, now) {
  const canon = buildCanon({events, anchors: run?.schedule?.anchors || {}, end: now});
  const verses = verseModel(events);
  const copies = new Map(), reach = new Map(), cleared = new Set(), over = new Set();
  let promoted = 0;
  for (const event of events) {
    if (event.type === 'checkpoint') copies.set(event.child, event.x_pos || 0);
    else if (event.type === 'experiment_result') {
      if (typeof event.x_pos === 'number') reach.set(event.sandbox, event.x_pos);
      if (event.outcome !== 'survived') over.add(event.sandbox);
    }
    else if (event.type === 'promote') promoted++;
    else if (event.type === 'stage_clear' || event.type === 'clear') cleared.add(event.sandbox);
  }
  const canonNames = new Set(canon.segments.map(segment => segment.name));
  // a split is still open while nothing after it has settled it
  const settled = new Set();
  let pending = null;
  for (const event of events) {
    if (event.type === 'multiverse') pending = event;
    else if (pending && ['promote', 'race_failed', 'failed', 'stopped', 'clear', 'stage_clear'].includes(event.type)) { settled.add(pending.t); pending = null; }
  }
  const stubsBetween = (from, to) => {
    const stubs = [];
    for (const event of events) {
      if (event.elapsed <= from || event.elapsed > to) continue;
      if (event.type === 'death') stubs.push({kind: 'present', reach: 0.8});
      else if (event.type === 'multiverse') {
        const base = copies.get(event.parent) || 0;
        for (const child of event.children || []) {
          if (canonNames.has(child)) continue;
          const got = reach.get(child);
          stubs.push({kind: 'verse', no: verses.get(child)?.no, open: !settled.has(event.t) && !over.has(child), reach: typeof got === 'number' ? Math.max(0.15, Math.min(1, (got - base) / 420)) : 0.5});
        }
      }
    }
    return stubs;
  };
  const folds = canon.segments.filter(segment => segment.next).map(segment => ({c: segment.c1, stubs: stubsBetween(segment.to, segment.next.from)}));
  // what is still open at the head: the present that just died, verses still trying
  const last = canon.segments.at(-1);
  const tail = last ? stubsBetween(last.to, Infinity) : [];
  const worlds = [];
  for (const segment of canon.segments) if (!worlds.length || worlds.at(-1).t0 !== segment.world) worlds.push({t0: segment.world, c: segment.c0});
  const stages = events.filter(event => event.type === 'stage_started').map(event => event.stage);
  worlds.forEach((world, index) => { world.label = index === 0 ? (events.find(event => event.type === 'created')?.stage || '1-1') : stages[index - 1] || ''; });
  const total = verses.size;
  const madeIt = [...verses.keys()].filter(name => cleared.has(name)).length;
  const trying = tail.filter(stub => stub.open).length;
  const finished = events.some(event => event.type === 'clear');
  // the verse that is canon right now has not collapsed: it is the present
  let present = true;
  for (const event of events) {
    if (event.type === 'death') present = false;
    else if (['created', 'promote', 'rewind', 'stage_started'].includes(event.type)) present = true;
  }
  const carrying = present && last && verses.has(last.name) && !cleared.has(last.name) ? 1 : 0;
  return {canon, folds, tail, worlds, total, promoted, madeIt, trying, finished, collapsed: Math.max(0, total - madeIt - trying - carrying), length: Math.max(canon.total, 0.001)};
}

// One sentence for the numbers, true for any run: finished, failed or still going.
function multiverseLine(model) {
  if (!model.total) return model.finished ? 'no split was needed: one machine, start to flag.' : '';
  const base = `${plural(model.total, 'verse')}. ${model.collapsed} collapsed.`;
  if (model.finished) return `${base} this is the one where ${G().hero} made it.`;
  return model.madeIt ? `${base} ${model.madeIt} ${G().reached}.` : base;
}

function hashOf(text) { let h = 2166136261; for (const ch of String(text)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; }

// Draws into a 2d context, in a box. Used by the closing card, the dock tab and the poster.
function drawMultiverse(ctx, model, box, options = {}) {
  const {x, y, w, h} = box;
  const seed = hashOf(options.seed || 'mario');
  const head = Math.min(model.length, options.head ?? model.length);
  const reachOf = h * 0.3;
  // Room past the head for whatever is still fanning out of it. A map that is still growing
  // keeps that room all the time, so the line never jumps when a split opens or settles.
  const padL = 14, padR = options.live || model.tail.length ? Math.max(22, reachOf * 1.12 + 8) : 22;
  const X = c => x + padL + c / model.length * (w - padL - padR);
  const wave = c => Math.sin(c / model.length * Math.PI * 2 * 1.3 + seed * 6.28) * 0.6 + Math.sin(c / model.length * Math.PI * 2 * 3.1 + seed * 17.3) * 0.4;
  const Y = c => y + h / 2 + wave(c) * h * 0.13;
  const unit = Math.max(1, w / 900);
  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  const fan = (c, stubs) => {
    const px = X(c), py = Y(c);
    const tangent = Math.atan2(Y(c + model.length / 200) - Y(c - model.length / 200), X(c + model.length / 200) - X(c - model.length / 200));
    // both sides of canon, packed tighter as one moment collects more of them, never folding back
    const step = Math.min(0.2, (1.45 - 0.42) / Math.max(1, Math.ceil(stubs.length / 2) - 1));
    stubs.forEach((stub, index) => {
      const side = index % 2 ? 1 : -1;
      const angle = tangent + side * (0.42 + step * Math.floor(index / 2));
      const uneven = stubs.length > 10 ? 0.84 + 0.3 * hashOf(`${seed}:${index}`) : 1;   // a crowd should not end on one arc
      const length = (0.3 + 0.7 * stub.reach) * reachOf * uneven * (stub.kind === 'present' ? 1.05 : 1);
      const ex = px + Math.cos(angle) * length, ey = py + Math.sin(angle) * length;
      const mx = px + Math.cos(tangent) * length * 0.5, my = py + Math.sin(tangent) * length * 0.5;
      const trying = Boolean(stub.open) && Boolean(options.live);
      ctx.strokeStyle = trying ? 'rgba(213,244,92,.55)' : stub.kind === 'present' ? 'rgba(255,255,255,.34)' : 'rgba(255,255,255,.22)';
      ctx.lineWidth = (stub.kind === 'present' ? 1.8 : 1.4) * unit;
      ctx.beginPath();
      ctx.moveTo(px, py);
      ctx.quadraticCurveTo(mx, my, ex, ey);
      ctx.stroke();
      if (trying) return;
      const r = 2.6 * unit;
      ctx.strokeStyle = '#d97a85';
      ctx.lineWidth = 1.5 * unit;
      ctx.beginPath();
      ctx.moveTo(ex - r, ey - r); ctx.lineTo(ex + r, ey + r);
      ctx.moveTo(ex + r, ey - r); ctx.lineTo(ex - r, ey + r);
      ctx.stroke();
    });
  };
  for (const fold of model.folds) if (fold.c <= head + 0.001) fan(fold.c, fold.stubs);
  if (model.tail.length && head >= model.length - 0.001) fan(head, model.tail);
  // canon
  ctx.strokeStyle = '#d5f45c';
  ctx.lineWidth = 3 * unit;
  ctx.beginPath();
  for (let step = 0; step <= 240; step++) {
    const c = head * step / 240;
    if (step) ctx.lineTo(X(c), Y(c)); else ctx.moveTo(X(c), Y(c));
  }
  ctx.stroke();
  for (const fold of model.folds) {
    if (fold.c > head + 0.001) continue;
    ctx.fillStyle = '#0e0e12';
    ctx.strokeStyle = '#d5f45c';
    ctx.lineWidth = 1.5 * unit;
    ctx.beginPath();
    ctx.arc(X(fold.c), Y(fold.c), 3 * unit, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }
  ctx.font = `500 ${Math.round(10 * unit)}px GeistPixelSquare, monospace`;
  ctx.fillStyle = 'rgba(255,255,255,.4)';
  for (const world of model.worlds) {
    if (world.c > head) continue;
    const text = G().goal(world.label), at = X(world.c) + 4 * unit;
    ctx.fillText(text, Math.min(at, x + w - ctx.measureText(text).width - 2 * unit), y + 12 * unit);   // never off the edge
  }
  ctx.fillStyle = '#d5f45c';
  ctx.beginPath();
  ctx.arc(X(head), Y(head), 5.5 * unit, 0, Math.PI * 2);
  ctx.fill();
  ctx.globalAlpha = 0.45;
  ctx.lineWidth = 2 * unit;
  ctx.beginPath();
  ctx.arc(X(head), Y(head), 10 * unit, 0, Math.PI * 2);
  ctx.stroke();
  ctx.restore();
}

// A canvas on the page, sharp on any screen.
function paintMultiverse(canvas, model, options) {
  const bounds = rect(canvas);
  const ratio = window.devicePixelRatio || 1;
  const w = Math.max(10, Math.round(bounds.width)), h = Math.max(10, Math.round(bounds.height));
  if (canvas.width !== w * ratio || canvas.height !== h * ratio) { canvas.width = w * ratio; canvas.height = h * ratio; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, w, h);
  drawMultiverse(ctx, model, {x: 0, y: 0, w, h}, options);
}
const runSeed = () => view?.source || String(view?.output || '').split('/').pop() || 'mario';

// The dock tab: the map so far, growing as the run goes.
function renderMultiverse() {
  const events = played();
  const now = run.drag ? run.drag.elapsed : view.elapsed || 0;
  const model = multiverseModel(events, now);
  const alive = view.timelines.filter(item => ['trunk', 'candidate'].includes(item.role) && item.phase !== 'dead').length;
  $('multiverse-kicker').textContent = model.worlds.length > 1 ? `worlds ${model.worlds[0].label} to ${model.worlds.at(-1).label} · so far` : `${G().goal(model.worlds[0]?.label || view.stage || G().stage)} · so far`;
  $('multiverse-count').textContent = `${plural(model.total, 'verse')} · ${alive} alive`;
  paintMultiverse($('multiverse-map'), model, {seed: runSeed(), live: true});
}

// The poster: the same map, square, with the numbers. For sharing.
async function drawMultiversePoster() {
  if (!view || !run) return null;
  await Promise.all([document.fonts.load('72px GeistPixelCircle'), document.fonts.load('24px GeistPixelSquare')]);
  const events = view.mode === 'replay' && run.schedule ? run.schedule.events : view.events;
  const model = multiverseModel(events, view.mode === 'replay' && run.schedule ? run.schedule.end : view.elapsed || 0);
  const size = 1200, canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#0e0e12';
  ctx.fillRect(0, 0, size, size);
  ctx.textBaseline = 'alphabetic';
  ctx.font = '500 84px GeistPixelCircle, monospace';
  let cursor = 70;
  for (const [text, color] of [[`${G().hero} `, '#fff'], ['never', '#d5f45c'], [' dies', '#fff'], ['.', '#d5f45c']]) { ctx.fillStyle = color; ctx.fillText(text, cursor, 150); cursor += ctx.measureText(text).width; }
  const deaths = events.filter(event => event.type === 'death').length;
  ctx.font = '500 28px GeistPixelSquare, monospace';
  ctx.fillStyle = 'rgba(255,255,255,.74)';
  ctx.fillText(`one run. ${multiverseLine(model)}`.trim(), 70, 210);
  drawMultiverse(ctx, model, {x: 50, y: 290, w: 1100, h: 640}, {seed: runSeed()});
  ctx.font = '500 22px GeistPixelSquare, monospace';
  ctx.fillStyle = 'rgba(255,255,255,.52)';
  ctx.fillText(`${plural(deaths, 'death')}${model.finished ? ' · 0 game overs' : ''} · every verse a whole microVM`, 70, size - 64);   // only a finished run gets to brag
  const credit = 'built on ', brand = 'microsandbox';
  const right = size - 70 - ctx.measureText(credit + brand).width;
  ctx.fillStyle = 'rgba(255,255,255,.74)';
  ctx.fillText(credit, right, size - 64);
  ctx.fillStyle = BRAND;
  ctx.fillText(brand, right + ctx.measureText(credit).width, size - 64);
  return canvas;
}
async function saveMultiversePoster() {
  const canvas = await drawMultiversePoster();
  if (!canvas) return;
  const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
  if (blob) saveBlob(blob, `${G().hero}-never-dies-multiverse-${runSeed()}.png`);
}

// ---------------------------------------------------------------- beats

function enqueue(event) {
  run.beats.push(event);
  if (!run.pumping) pump();
}

async function pump() {
  const owner = run, epoch = run.pumpEpoch;
  run.pumping = true;
  while (run === owner && epoch === run.pumpEpoch && run.beats.length) {
    const event = run.beats.shift();
    run.playedT = event.t;
    try { await beat(event); } catch (error) { console.error(error); }
  }
  if (run === owner && epoch === run.pumpEpoch) run.pumping = false;
}

function timelineOf(name) {
  return view?.timelines.find(item => item.name === name);
}

async function beat(event) {
  const owner = run, epoch = run.pumpEpoch;
  const current = () => run === owner && run.pumpEpoch === epoch;
  const stage = event.stage || '1-1';
  pushOps(opsFor(event));
  switch (event.type) {
    case 'created': {
      setFocus(G().machine);
      later(3200, 'jev reads the game’s memory and picks a move. every eight frames.');
      return;
    }
    case 'checkpoint': {
      run.counts.snapshots += 1;
      bump('snapshots');
      const first = run.counts.snapshots === 1;
      setFocus(first
        ? 'snapshot. microsandbox <em class="ours">freezes a copy of the whole running machine.</em>'
        : 'snapshot. another frozen copy of the whole machine.');
      $('screen').classList.remove('flash');
      void $('screen').offsetWidth;
      $('screen').classList.add('flash');
      renderMap();
      flashNode(event.child);
      return;
    }
    case 'death': {
      run.counts.deaths += 1;
      bump('deaths');
      run.lock = {mode: 'single', names: [event.sandbox]};
      syncScreen();
      markDead(event.sandbox);
      deathRing(event);
      setFocus(`${esc(G().died)} <em>this verse ends here.</em>`);
      await hold('death');
      return;
    }
    case 'retry_hypothesis': {
      if (event.action) setFocus(`jev tried <s>${esc(LABEL[event.action] || event.action)}</s> here. next time it picks something else.`);
      await hold('retry_hypothesis');
      return;
    }
    case 'rewind': {
      run.counts.rewinds += 1;
      bump('rewinds');
      run.lock = null;
      rewindSweep(event);
      await rewindVeil(event.parent, event.branch_ms, event.manual);
      if (!current()) return;
      const branch = typeof event.branch_ms === 'number' ? ` — <em class="ours">${Math.round(event.branch_ms)} ms</em> later ${G().alive}` : '';
      setFocus(event.manual ? `you picked the snapshot. microsandbox branches it${branch}.` : `rewind. branch the frozen copy${branch}.`);
      return;
    }
    case 'rewind_refused': {
      setFocus('that copy is gone. pick a snapshot that is still frozen.');
      await hold('rewind_refused');
      return;
    }
    case 'multiverse': {
      run.counts.forks += 1;
      run.counts.verses += (event.children || []).length;
      bump('verses');
      run.lock = {mode: 'grid', names: event.children};
      forkRing(event.parent);
      forkFocus = event.t;
      setFocus(`split. <em>${event.children?.length === 4 ? 'four verses' : `${event.children?.length || 4} verses`}</em> from one moment.`);
      await splitScreen(event.children);
      return;
    }
    case 'promote': {
      run.winner = event.sandbox;
      syncScreen();
      setFocus(`${esc(verseName(event.sandbox))} made it. <em>it is canon now.</em>`);
      await hold('promote');
      if (!current()) return;
      run.lock = null;
      await mergeScreen(event.sandbox);
      if (!current()) return;
      run.winner = null;
      return;
    }
    case 'race_failed': {
      setFocus('all of them collapsed. split again.');
      run.lock = null;
      await hold('race_failed');
      return;
    }
    case 'experiment_result': {
      if (event.outcome !== 'dead' || !verseOf(event.sandbox)) return;
      const verse = verseOf(event.sandbox);
      setFocus(`verse ${verse.no}${verse.trait ? ` (${esc(verse.trait)})` : ''} collapsed.`);
      return;
    }
    case 'paused': {
      setFocus('paused. <em class="ours">the whole machine is frozen mid-frame.</em> not the emulator — the machine.');
      return;
    }
    case 'resumed': {
      setFocus('resumed. the machine picks up exactly where it stopped.');
      return;
    }
    case 'stage_clear': {
      stamp(`${G().goal(stage)} clear`);
      setFocus(`${esc(G().flag(stage))} ${game === 'mario' ? `world ${esc(stage)} ` : ''}cleared with <em>${plural(run.counts.deaths, 'death')}</em> and zero game overs.`);
      await hold('stage_clear');
      return;
    }
    case 'stage_started': {
      run.lock = null;
      setFocus(`${esc(G().goal(stage))}. a new machine, new snapshots.`);
      return;
    }
    case 'clear': {
      stamp(`${G().goal(stage)} clear`);
      setFocus(`${esc(G().flag(stage))} <em>${plural(run.counts.deaths, 'death')}</em>, zero game overs.`);
      await hold('clear');
      if (!current()) return;
      const cleared = [...new Set([...(view.completed_stages || []), stage])];
      const branch = played().map(item => item.branch_ms).filter(value => typeof value === 'number');
      const typical = branch.length ? Math.round(branch.reduce((sum, value) => sum + value, 0) / branch.length / 10) * 10 : null;
      finale(`${cleared.length > 1 ? `world ${cleared[0]} to ${cleared.at(-1)}` : G().goal(stage)} cleared`, `${G().hero} never died.`,
        `${multiverseLine(multiverseModel(view.events, view.elapsed || 0))} every one was a whole running machine, branched by microsandbox${typical ? ` in about ${typical} ms` : ''}.`);
      return;
    }
    case 'failed': {
      finale('run stopped', 'something broke.', event.error || 'the controller reported a failure.');
      return;
    }
    case 'stopped': {
      if (leaving) return;                                   // on the way back to the lobby
      finale('run stopped', 'stopped.', `${plural(run.counts.deaths, 'death')} so far. all machines cleaned up.`);
      return;
    }
    default:
      return;
  }
}

function finale(eyebrow, title, dek) {
  $('finale-eyebrow').textContent = eyebrow;
  $('finale-title').innerHTML = esc(title).replace(/\.$/, '<em>.</em>');
  $('finale-dek').innerHTML = esc(dek).replace('microsandbox', '<span class="ours">microsandbox</span>');
  // the map of every verse this run created, under the words
  const model = multiverseModel(view.events, view.elapsed || 0);
  $('finale-map').hidden = !model.total;
  $('finale-poster').hidden = !model.total;
  if (model.total) requestAnimationFrame(() => paintMultiverse($('finale-map'), model, {seed: runSeed()}));
  $('scrub').hidden = view?.mode !== 'replay';
  $('finale-download').hidden = !recordingId();
  $('finale').hidden = false;
}

// After a seek the feed jumped; rebuild the presentation without replaying every beat.
function catchUp() {
  run.peekSeq++;
  run.pumpEpoch++;
  run.pumping = false;
  run.graph = null;
  if (!run.drag) clearPeek();
  for (const veil of $('screen').querySelectorAll('.veil')) veil.remove();
  run.beats = [];
  run.lock = null;
  run.winner = null;
  run.counts = {snapshots: 0, deaths: 0, rewinds: 0, forks: 0, verses: 0};
  run.ops = [];
  run.opsRendered = 0;
  for (const event of view.events) { tally(event); run.ops.push(...opsFor(event)); }
  renderOps();
  run.lastEventT = run.playedT = view.events.at(-1)?.t || 0;
  $('finale').hidden = true;
  // Landing on the end of a run (stopping a paused replay, seeking to its last second)
  // still deserves the closing card; nothing else will play that beat.
  const last = view.events.at(-1);
  if (last && ENDED.has(view.status) && ['stopped', 'failed', 'clear', 'stage_clear'].includes(last.type)) beat(last);
  if (run.drag?.released && Math.abs(view.elapsed - run.drag.elapsed) < 0.75) { run.drag = null; run.dragT = null; if (run.peek && !run.peek.pinned) { run.peek = null; renderPeek(); } }
  const hopped = canonOn() && performance.now() - (run.hopAt || 0) < 1500;
  if (!hopped) setFocus(run.scan ? scanCaption() : view.paused ? 'paused here. press space to play.' : 'resumed from here.');
}

// ---------------------------------------------------------------- frames (cache + smooth playback)

const frameCache = new Map();   // "name:frame" -> HTMLImageElement (loaded) | null (failed)
const framePending = new Map(); // "name:frame" -> Promise

function frameKey(name, frame) { return `${name}:${frame}`; }
const peekContext = $('peek-canvas').getContext('2d');
let peekWanted = null;

function drawPeek(name, frame) {
  const key = frameKey(name, frame);
  peekWanted = key;
  const cached = frameCache.get(key);
  const [w, h] = G().size;
  if (cached) { peekContext.drawImage(cached, 0, 0, w, h); return; }
  peekContext.clearRect(0, 0, w, h);
  fetchFrame(name, frame).then(image => {
    if (peekWanted !== key) return;
    if (image) peekContext.drawImage(image, 0, 0, w, h);
    else $('peek-hint').textContent = 'frame not recorded';
  });
}

// ``run`` reads a recording that is not the one being played (the lobby, a download).
function fetchFrame(name, frame, run = null) {
  const key = frameKey(name, frame);
  if (frameCache.has(key)) return Promise.resolve(frameCache.get(key));
  if (framePending.has(key)) return framePending.get(key);
  const promise = new Promise(resolve => {
    const image = new Image();
    image.onload = () => { frameCache.set(key, image); framePending.delete(key); resolve(image); };
    image.onerror = () => { frameCache.set(key, null); framePending.delete(key); resolve(null); };
    image.src = `/api/frame/${encodeURIComponent(name)}?f=${frame}${run ? `&run=${run}` : ''}`;
  });
  framePending.set(key, promise);
  if (frameCache.size > 4000) for (const old of [...frameCache.keys()].slice(0, 1500)) frameCache.delete(old);
  return promise;
}

function tileFor(name) {
  let tile = run.tiles.get(name);
  if (!tile) {
    const element = document.createElement('div');
    element.className = 'tile';
    element.dataset.name = name;
    const canvas = document.createElement('canvas');
    [canvas.width, canvas.height] = G().size;
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', `${G().title} running inside a microsandbox vm`);
    const tag = document.createElement('div');
    tag.className = 'tag';
    element.append(canvas, tag);
    tile = {element, canvas, context: canvas.getContext('2d'), tag, shown: null, anchor: null, rate: 0, limit: Infinity, src: ''};
    run.tiles.set(name, tile);
  }
  return tile;
}

function drawTile(tile, name, frame) {
  if (tile.shown === frame) return;
  const cached = frameCache.get(frameKey(name, frame));
  if (cached) {
    tile.context.drawImage(cached, 0, 0, tile.canvas.width, tile.canvas.height);
    tile.shown = frame;
    tile.src = cached.src;
    return;
  }
  fetchFrame(name, frame).then(image => {
    if (image && tile.wanted === frame) drawTile(tile, name, frame);
  });
  tile.wanted = frame;
}

function preload(name, from, count) {
  for (let step = 1; step <= count; step++) fetchFrame(name, from + step * frameStep());
}

// Between server updates each playing tile keeps moving at the replay's frame rate.
function animate() {
  if (run && view) {
    const now = performance.now();
    for (const [name, tile] of run.tiles) {
      if (!tile.element.isConnected || !tile.anchor) continue;
      let frame = tile.anchor.frame;
      if (tile.rate > 0) {
        const lead = Math.min((now - tile.anchor.at) / 1000, LEAD_SECONDS);
        frame = Math.min(tile.limit, tile.anchor.frame + lead * tile.rate);
      }
      frame = Math.floor(frame / frameStep()) * frameStep();
      if (tile.shown !== null && frame < tile.shown && tile.shown - frame < 8) frame = tile.shown;
      drawTile(tile, name, frame);
      if (tile.rate > 0) preload(name, frame, 16);
    }
  }
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);

// ---------------------------------------------------------------- screen (game tiles)

function played() {
  return view.events.filter(event => event.t <= run.playedT);
}

function playedTrunk(events) {
  const last = [...events].reverse().find(event => ['created', 'rewind', 'promote'].includes(event.type));
  return last ? last.sandbox : view.trunk;
}

function currentShow() {
  if (run.lock) return run.lock;
  const events = played();
  const trunk = playedTrunk(events);
  const race = [...events].reverse().find(event => event.type === 'multiverse');
  const promoted = race && events.some(event => event.type === 'promote' && event.t > race.t);
  const raceOver = race && events.some(event => ['race_failed', 'rewind', 'stage_started'].includes(event.type) && event.t > race.t);
  if (race && !promoted && !raceOver && race.children.some(name => timelineOf(name)?.role === 'candidate')) return {mode: 'grid', names: race.children};
  return {mode: 'single', names: trunk ? [trunk] : []};
}

function syncScreen() {
  const screen = $('screen');
  const show = currentShow();
  screen.classList.toggle('grid', show.mode === 'grid');
  screen.classList.toggle('paused', Boolean(view.paused) && view.mode !== 'replay');
  const wanted = show.names.map(name => tileFor(name).element);
  const present = [...screen.children].filter(node => node.classList.contains('tile'));
  if (present.length !== wanted.length || present.some((node, index) => node !== wanted[index])) {
    for (const node of present) if (!wanted.includes(node)) node.remove();
    for (const node of wanted) if (node.parentElement !== screen) screen.append(node);
    screen.append(...wanted);
  }
  const smooth = view.mode === 'replay' && !view.paused;
  const known = show.mode === 'grid' ? canonWinner() : null;
  show.names.forEach((name, index) => {
    const tile = tileFor(name);
    const item = timelineOf(name);
    if (!item) return;
    const dead = item.role === 'dead' || item.phase === 'dead';
    const playing = item.phase === 'playing' && ['trunk', 'candidate'].includes(item.role);
    if (!tile.anchor || item.frame >= (tile.shown ?? -1) || tile.shown - item.frame >= 8) tile.anchor = {frame: item.frame, at: performance.now()};
    else tile.anchor = {frame: tile.shown, at: performance.now()};
    tile.rate = smooth && playing ? (view.fps || 60) * (view.speed || 1) : 0;
    tile.limit = playing ? Infinity : item.frame;
    if (tile.rate === 0) drawTile(tile, name, item.frame);
    if (dead && !tile.wasDead && show.mode === 'grid') {
      tile.element.classList.add('collapsing');
      setTimeout(() => tile.element.classList.remove('collapsing'), 360);
    }
    tile.wasDead = dead;
    tile.element.classList.toggle('dead', dead);
    tile.element.classList.toggle('winner', run.winner === name || known === name);
    tile.element.classList.toggle('loser', Boolean(run.winner) && run.winner !== name);
    tile.element.classList.toggle('ghost', Boolean(known) && known !== name);
    const limit = item.role === 'timed_out' ? 'time limit' : item.role === 'approach_exhausted' ? 'approach limit' : '';
    if (show.mode === 'grid') tile.tag.textContent = `${verseTitle(name)}${dead ? ' ✕' : limit ? ` · ${limit}` : run.winner === name ? ' · canon' : ''}`;
    else tile.tag.textContent = dead ? 'dead' : limit || ((view.paused && view.mode !== 'replay') ? 'paused · machine frozen' : '');
  });
  renderJev(show);
  renderPeek();
}

function markDead(name) {
  const tile = run.tiles.get(name);
  if (tile) tile.element.classList.add('dead');
}

function renderJev(show) {
  const jev = $('jev');
  const active = show.mode === 'single' ? timelineOf(show.names[0]) : null;
  const action = active?.action;
  jev.hidden = !active || !action;
  if (!active || !action) return;
  $('move').textContent = LABEL[action] || action;
  $('latency').textContent = typeof active.latency_ms === 'number' ? `${(active.latency_ms / 1000).toFixed(2)} s` : '';
  $('pad-right').classList.toggle('on', action.startsWith('right'));
  $('pad-left').classList.toggle('on', action.startsWith('left'));
  $('pad-up').classList.toggle('on', action === 'up');
  $('pad-down').classList.toggle('on', action === 'down');
  $('pad-a').classList.toggle('on', action.includes('jump'));
  $('pad-b').classList.toggle('on', action.includes('run'));
  $('pad-flap').classList.toggle('on', action === 'flap');
}

function renderPlayback(running) {
  // At a flag the transport gives way to the intermission: the run waits for "next world".
  const held = running && view?.intermission ? view.intermission : null;
  $('playback').hidden = !running;
  $('transport').hidden = !running || Boolean(held);
  $('intermission').hidden = !held;
  if (held && run) {
    $('inter-text').innerHTML = `${esc(G().goal(held.stage))} clear · <em>${plural(run.counts.deaths, 'death')}</em>`;
    $('inter-next').title = `${G().goal(held.next)} · enter`;
    $('inter-next').disabled = Boolean(exporting);           // a download reads the run while it is at rest
    if (!run.heldAt) { run.heldAt = held.stage; setFocus('stopped at the flag. <em class="ours">the whole machine is frozen</em> until you say go.'); scheduleClearCard(held); }
    $('cc-next').disabled = Boolean(exporting);
  } else if (run) { run.heldAt = null; hideClearCard(); }
  // The bar in the transport row is what is left once the overlay has been put away.
  $('intermission').hidden = !held || !$('clearcard').hidden || Boolean(clearCardTimer);
  $('transport').hidden = !running || (Boolean(held) && $('intermission').hidden === false);
  $('tp-markers').hidden = !running;
  $('scrub-hint').hidden = !(running && !hintDone);
  $('frame').classList.toggle('hot', Boolean(running));
  syncSound(running);
  if (!running) $('tp-clock').textContent = '';
  if (!running || !view || !run) return;
  const replay = view.mode === 'replay';
  const scan = run.scan;
  const isMoving = moving();
  // SVG elements have no `hidden` property, only the attribute.
  $('ic-pause').toggleAttribute('hidden', !isMoving);
  $('ic-play').toggleAttribute('hidden', isMoving);
  const pauseLabel = isMoving ? 'pause · space' : (!replay && run.peek?.pinned ? 'play the recording from here · space' : 'play · space');
  $('tp-pause').setAttribute('aria-label', pauseLabel);
  $('tp-pause').title = pauseLabel;
  const t = viewT();
  $('tp-rew').disabled = t <= (run.geometry?.t0 || 0) + 0.05 && !scan;
  $('tp-back').disabled = $('tp-rew').disabled;
  $('tp-rew').classList.toggle('on', Boolean(scan && scan.dir < 0));
  $('tp-rew').title = scan?.dir < 0 ? `rewinding ${scan.ramp}× · ← for faster` : 'rewind · ←';
  const ffOn = scan ? scan.dir > 0 && scan.ramp > 1 : replay && !view.paused && (view.speed || 1) > 1;
  $('tp-ff').classList.toggle('on', ffOn);
  $('tp-ff').disabled = replay ? atEnd() : !(scan || run.peek?.pinned);
  $('tp-skip').disabled = $('tp-ff').disabled;
  $('tp-skip').title = $('tp-skip').disabled && !replay ? 'ahead 10 s · nothing ahead of live' : 'ahead 10 s · shift+→';
  $('tp-ff').title = !replay && $('tp-ff').disabled ? 'fast-forward · nothing ahead of live' : ffOn ? `fast forward ${scan ? scan.ramp : view.speed}× · → for faster` : 'fast-forward · →';
  renderClock();
  // The speed menu: a replay's speed, or how fast the recording plays from a moment behind live.
  const behind = Boolean(scan) || Boolean(run.peek?.pinned);
  $('tp-speed').hidden = !(replay || behind);
  const rate = replay ? (view.speed || 1) : (scan?.dir > 0 ? scan.ramp : (run.playRate || 1));
  if (menuValue($('tp-speed')) !== String(rate)) setMenuValue($('tp-speed'), rate);
  $('tp-speed').classList.toggle('on', rate !== 1);
  $('tp-download').hidden = !replay || Boolean(exporting);
  // skip deaths: only a recording knows which verse survives, and only one with deaths needs it
  const cuttable = replay && Boolean(run.canon?.folds.length);
  if (!cuttable && skipDeaths && replay && run.canon) skipDeaths = false;
  $('tp-canon').hidden = !cuttable;
  $('tp-canon').classList.toggle('on', skipDeaths);
  $('tp-canon').setAttribute('aria-pressed', String(skipDeaths));
  renderNow();
}

// The live beacon: pulsing while the machine plays, still while it is paused, and a
// "back to live / now" button whenever you are looking at the past.
function renderNow() {
  const button = $('tp-now');
  if (!view || !run) { button.hidden = true; return; }
  const running = view.busy || ['starting', 'running', 'paused'].includes(view.status);
  const replay = view.mode === 'replay';
  const lookingBack = Boolean(run.peek) || Boolean(run.drag);
  if (!running || (replay && (!lookingBack || run.scan))) { button.hidden = true; return; }
  button.hidden = false;
  button.classList.toggle('back', lookingBack);
  button.classList.toggle('at', !lookingBack);
  button.classList.toggle('still', !lookingBack && Boolean(view.paused));
  button.setAttribute('aria-label', lookingBack ? (replay ? 'back to now' : 'back to live') : 'live');
  $('tp-now-label').textContent = lookingBack ? (replay ? 'back to now' : 'back to live') : view.paused ? 'live · paused' : 'live';
}
$('tp-now').onclick = () => { if (run?.peek || run?.drag) clearPeek(); };

function rect(node) { return node.getBoundingClientRect(); }

// A snapshot landing: the new thumbnail flashes for a beat. No flight.
function flashNode(slot) {
  const group = document.getElementById(`group-${slot}`);
  if (!group) return;
  group.classList.add('landed');
  setTimeout(() => group.classList.remove('landed'), 140);
}

async function rewindVeil(slot, branchMs, manual) {
  const screen = $('screen');
  const veil = document.createElement('div');
  veil.className = 'veil';
  const img = document.createElement('img');
  img.alt = '';
  img.src = `/api/frame/${encodeURIComponent(slot)}?f=slot`;
  const label = document.createElement('div');
  label.className = 'label';
  label.innerHTML = `◀◀ rewind<small>${manual ? 'your snapshot' : 'branch the frozen copy'}${typeof branchMs === 'number' ? ` · ${Math.round(branchMs)} ms` : ''}</small>`;
  veil.append(img, label);
  screen.append(veil);
  syncScreen();
  const total = (view?.holds?.rewind ?? DEFAULT_HOLDS.rewind) * 1000 / (view?.mode === 'replay' ? (view.speed || 1) : 1);
  const visual = Math.min(450, total);
  await sleep(visual);
  veil.remove();
  await sleep(Math.max(0, total - visual));
}

async function splitScreen(children) {
  const single = [...$('screen').children].find(node => node.classList.contains('tile'));
  const from = single ? rect(single) : null;
  syncScreen();
  if (!from) return;
  // Four copies of the one picture lie on top of each other, drift apart like a double
  // exposure, then each settles into its own corner.
  const drift = Math.max(8, from.width * 0.028);
  const screen = $('screen');
  screen.classList.add('splitting');
  const animations = children.map((name, index) => {
    const tile = run.tiles.get(name)?.element;
    if (!tile) return null;
    const to = rect(tile);
    const stacked = `translate(${from.left - to.left}px, ${from.top - to.top}px) scale(${from.width / to.width}, ${from.height / to.height})`;
    const dx = (index % 2 ? 1 : -1) * drift, dy = (index < 2 ? -1 : 1) * drift;
    const apart = `translate(${from.left - to.left + dx}px, ${from.top - to.top + dy}px) scale(${from.width / to.width}, ${from.height / to.height})`;
    return tile.animate([
      {transform: stacked, transformOrigin: 'top left', offset: 0},
      {transform: apart, transformOrigin: 'top left', offset: 0.38, easing: 'cubic-bezier(.4,0,.2,1)'},
      {transform: 'none', transformOrigin: 'top left', offset: 1},
    ], {duration: 380, easing: 'ease-out'}).finished;
  });
  setTimeout(() => screen.classList.remove('splitting'), 190);
  await Promise.all(animations.filter(Boolean));
  screen.classList.remove('splitting');
  await hold('multiverse');
}

async function mergeScreen(winner) {
  const tile = run.tiles.get(winner)?.element;
  const from = tile ? rect(tile) : null;
  // The other verses stay where they were, as ghosts, while the survivor grows over them.
  const screen = $('screen'), frame = rect(screen);
  const ghosts = document.createElement('div');
  ghosts.className = 'ghosts';
  for (const other of [...screen.children].filter(node => node.classList.contains('tile') && node !== tile)) {
    const box = rect(other), picture = document.createElement('canvas');
    picture.width = 256;
    picture.height = 240;
    try { picture.getContext('2d').drawImage(other.querySelector('canvas'), 0, 0); } catch {}
    picture.className = 'ghost-picture';
    picture.style.left = `${box.left - frame.left}px`;
    picture.style.top = `${box.top - frame.top}px`;
    picture.style.width = `${box.width}px`;
    picture.style.height = `${box.height}px`;
    ghosts.append(picture);
  }
  syncScreen();
  if (!tile || !from || tile.parentElement !== screen) return;
  screen.append(ghosts);
  tile.classList.add('rising');
  const to = rect(tile);
  try {
    await tile.animate([
      {transform: `translate(${from.left - to.left}px, ${from.top - to.top}px) scale(${from.width / to.width}, ${from.height / to.height})`, transformOrigin: 'top left'},
      {transform: 'none', transformOrigin: 'top left'},
    ], {duration: 300, easing: 'cubic-bezier(.3,0,.1,1)'}).finished;
  } finally {
    tile.classList.remove('rising');
    ghosts.remove();
  }
}

// ---------------------------------------------------------------- timeline effects

function fx(id, tag, attrs, lifetime) {
  const svg = $('map');
  document.getElementById(id)?.remove();
  const node = document.createElementNS(SVG, tag);
  node.id = id;
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  svg.append(node);
  setTimeout(() => node.remove(), lifetime);
  return node;
}

function deathRing(event) {
  const geometry = run.geometry;
  if (!geometry) return;
  fx('fx-death', 'circle', {cx: geometry.X(event.elapsed), cy: geometry.presentY, r: 7, class: 'fx-ring red'}, 1100);
}

// The rewind beat on the line: the copy that is about to become the present pulses.
// The line itself, resuming from that copy, says the rest.
function rewindSweep(event) {
  if (run.geometry?.nodes.some(item => item.slot === event.parent)) pulseNode(event.parent, 900);
}

function forkRing(slot) {
  const geometry = run.geometry;
  const node = geometry?.nodes.find(item => item.slot === slot);
  if (!node) return;
  fx('fx-fork', 'circle', {cx: node.px, cy: geometry.presentY, r: 30, class: 'fx-ring'}, 2600);
  pulseNode(slot, 1200);
}

function pulseNode(slot, lifetime) {
  const group = document.getElementById(`group-${slot}`);
  if (!group) return;
  group.classList.add('branching');
  setTimeout(() => group.classList.remove('branching'), lifetime);
}

// ---------------------------------------------------------------- looking back (peek) and selecting

async function traceFor(name) {
  const owner = run;
  const cached = owner.traces.get(name);
  const alive = ['trunk', 'candidate'].includes(timelineOf(name)?.role);
  if (cached && (!alive || Date.now() - cached.at < 1500)) return cached.samples;
  try {
    const {samples} = await (await fetch(`/api/trace/${encodeURIComponent(name)}`)).json();
    owner.traces.set(name, {samples, at: Date.now()});
    return samples;
  } catch {
    return cached?.samples || [];
  }
}

function nearestSample(samples, t) {
  let best = null, distance = Infinity;
  for (const sample of samples) {
    const d = Math.abs(sample[2] - t);
    if (d < distance) { distance = d; best = sample; }
  }
  return best;
}

function setPeek(next) {
  if (run.peek?.pinned && !next?.pinned && next?.name !== run.peek.name) return;
  const before = run.peek?.pinned ? (run.peek.slot || run.peek.name) : null;
  run.peek = next;
  if (next?.pinned) dockTab = 'moment';
  renderPeek();
  if (before !== (next?.pinned ? (next.slot || next.name) : null)) renderMap();
  else renderDock();
}

function clearPeek() {
  if (!run) return;
  endScanQuiet();
  run.peekSeq++;
  const wasPinned = Boolean(run.peek?.pinned);
  run.peek = null;
  renderPeek();
  if (wasPinned) renderMap();
}

function renderPeek() {
  const peek = run?.peek;
  const box = $('peek');
  renderNow();
  if (!peek) { box.hidden = true; $('pin').hidden = true; return; }
  if (peek.slot) drawPeek(peek.slot, 'slot');
  else drawPeek(peek.name, peek.frame);
  // Warm the frames next to this one: both ways when stepping, only ahead while scanning.
  const ahead = run.scan ? run.scan.dir * 4 : 6;
  if (peek.samples) for (let step = -6; step <= 6; step++) { const sample = peek.samples[peek.index + step]; if (step && sample && (ahead > 0 ? step <= ahead : step >= ahead) && (run.scan ? Math.sign(step) === Math.sign(ahead) : true)) fetchFrame(peek.name, sample[0]); }
  box.classList.toggle('dead', Boolean(peek.dead));
  const frozen = peek.slot && view.mode !== 'replay' && view.busy && timelineOf(peek.slot)?.role === 'checkpoint';
  const what = peek.slot ? `${frozen ? 'frozen copy' : view.mode === 'replay' ? 'recorded checkpoint' : 'old snapshot'} · ${short(peek.slot)} · ` : '';
  $('peek-tag').textContent = `${what}${typeof peek.elapsed === 'number' ? `${clock(peek.elapsed)} · ` : ''}frame ${peek.frame} · x ${peek.x}`;
  $('peek-hint').textContent = peek.pinned ? ', . step a frame · esc back' : run.drag || run.scan ? '' : 'click to pin';
  box.hidden = false;
  if (peek.pinned) {
    const replay = view?.mode === 'replay';
    const retained = Boolean(peek.slot) && view.busy && !replay && timelineOf(peek.slot)?.role === 'checkpoint';
    $('peek-seek').hidden = !replay;
    $('peek-rewind').hidden = replay || !retained;
    $('peek-label').textContent = replay ? (peek.slot ? 'checkpoint' : '') : retained ? 'still frozen' : peek.slot ? 'old snapshot' : '';
  }
  renderPin();
}

// The popover sits on the timeline, above (or below) the point that was pinned.
// The popover sits on the timeline, above (or below) the point that was pinned.
function renderPin() {
  const pin = $('pin');
  const peek = run?.peek;
  const geometry = run?.geometry;
  const actions = view?.mode === 'replay' || !$('peek-rewind').hidden;
  if (!peek?.pinned || !geometry || !actions) { pin.hidden = true; return; }
  const svg = $('map'), wrap = svg.closest('.map-wrap');
  const s = rect(svg), w = rect(wrap);
  const node = peek.slot ? geometry.nodes.find(item => item.slot === peek.slot) : null;
  const px = node ? node.px : geometry.X(peek.elapsed);
  const py = node ? geometry.presentY - node.h / 2 : (peek.anchorY ?? geometry.presentY);
  if (s.left - w.left + px < 0 || s.left - w.left + px > w.width || peek.elapsed < geometry.t0 - 0.001) { pin.hidden = true; return; }
  pin.hidden = false;
  const below = s.top - w.top + py < 60;
  pin.classList.toggle('below', below);
  const left = Math.max(pin.offsetWidth / 2 + 4, Math.min(w.width - pin.offsetWidth / 2 - 4, s.left - w.left + px));
  pin.style.left = `${left}px`;
  pin.style.top = `${s.top - w.top + (below ? (node ? py + node.h + 4 : py + 6) : py - 4)}px`;
}

async function peekAt(target, pinned, still = () => true) {
  if (!target) return;
  const owner = run, seq = ++run.peekSeq;
  if (target.slot) {
    setPeek({name: target.sandbox, slot: target.slot, frame: target.frame, x: target.x, elapsed: target.elapsed, pinned, dead: false, samples: null});
    return;
  }
  const samples = await traceFor(target.name);
  if (run !== owner || seq !== run.peekSeq || !still()) return;
  const sample = nearestSample(samples, target.t);
  if (!sample) return;
  setPeek({name: target.name, frame: sample[0], x: sample[1], elapsed: sample[2], pinned, dead: target.dead, samples, index: samples.indexOf(sample), anchorY: target.y});
}

function stepPeek(direction) {
  const peek = run.peek;
  if (!peek?.samples) return;
  const index = Math.max(0, Math.min(peek.samples.length - 1, peek.index + direction));
  const sample = peek.samples[index];
  setPeek({...peek, frame: sample[0], x: sample[1], elapsed: sample[2], index});
}

function selectSlot(slot) {
  const node = run?.graph?.nodes.get(slot);
  if (!node) return;
  endScanQuiet();
  run.peek = null;
  peekAt({...node, sandbox: node.on, kind: 'node'}, true);
}

// Select a future at the moment it was decided: where it died, where it was promoted, or
// where the race ended — never a frame from the survivor's later life.
async function selectBranch(branch) {
  if (!run || !branch) return;
  endScanQuiet();
  const owner = run, seq = ++run.peekSeq;
  const samples = await traceFor(branch.name);
  if (run !== owner || seq !== run.peekSeq) return;
  const finish = played().find(event => event.t > (branch.event?.t ?? 0) &&
    ((event.type === 'promote' && event.sandbox === branch.name) ||
     (event.type === 'race_failed' && branch.event?.type === 'multiverse') ||
     (event.type === 'death' && event.sandbox === branch.name)));
  const cutoff = finish?.elapsed ?? view.elapsed;
  const sample = samples.filter(row => row[2] <= cutoff).at(-1) || samples.at(-1);
  if (!sample) return;
  run.peek = null;
  setPeek({name: branch.name, frame: sample[0], x: sample[1], elapsed: sample[2], pinned: true, dead: finish?.type === 'promote' ? false : Boolean(branch.dead), samples, index: samples.indexOf(sample), anchorY: laneYOf(branch.name)});
}

// Which timeline and level x the pointer is over, using what renderMap last drew.
// ---------------------------------------------------------------- zoom
//
// The wheel widens the timeline around the pointer; the wider line scrolls sideways
// (trackpad, shift+wheel or the bar). The head keeps itself in view unless you scrolled away.

const scroller = $('map-scroll');
function applyZoom() {
  const zoom = Math.max(1, run?.zoom || 1);
  const wanted = zoom > 1 ? `${Math.round(scroller.clientWidth * zoom)}px` : '';
  if ($('map').style.width !== wanted) $('map').style.width = wanted;
}
function headVisible() {
  const hx = run?.geometry?.head?.px;
  if (typeof hx !== 'number') return true;
  return hx >= scroller.scrollLeft && hx <= scroller.scrollLeft + scroller.clientWidth;
}
let autoScrolledAt = 0;
function followHead() {
  if (!run || (run.zoom || 1) <= 1) return;
  if (headVisible()) run.follow = true;        // once the head is in view, keep it there
  if (!run.follow) return;
  const hx = run.geometry?.head?.px;
  if (typeof hx !== 'number') return;
  const left = scroller.scrollLeft, width = scroller.clientWidth;
  if (hx > left + width - 40 || hx < left + 10) {
    autoScrolledAt = performance.now();
    scroller.scrollLeft = Math.max(0, Math.round(hx - width * 0.7));
  }
}
scroller.addEventListener('wheel', event => {
  const geometry = run?.geometry;
  if (!geometry || !view) return;
  const sideways = Math.abs(event.deltaX) > Math.abs(event.deltaY) && !event.ctrlKey;
  if (sideways) return;                                   // the browser pans the line
  event.preventDefault();
  const bounds = rect($('map'));
  const t = geometry.invert(event.clientX - bounds.left);
  const factor = Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0025));
  const ceiling = Math.max(1, 40 * geometry.span / Math.max(1, scroller.clientWidth - 80));   // up to 40 px per second
  const next = Math.min(ceiling, Math.max(1, (run.zoom || 1) * factor));
  if (Math.abs(next - (run.zoom || 1)) < 1e-6) return;
  run.zoom = next;
  applyZoom();
  renderMap();
  autoScrolledAt = performance.now();
  scroller.scrollLeft = Math.max(0, Math.round(run.geometry.X(t) - (event.clientX - rect(scroller).left)));
  run.follow = headVisible();
  renderPin();
}, {passive: false});
scroller.addEventListener('scroll', () => {
  if (!run) return;
  if (performance.now() - autoScrolledAt > 120) run.follow = headVisible();
  renderPin();
});

// Which timeline and moment the pointer is over, using what renderMap last drew.
function hitTest(clientX, clientY, options = {}) {
  const geometry = run?.geometry;
  if (!geometry) return null;
  const bounds = rect($('map'));
  const px = clientX - bounds.left, py = clientY - bounds.top;
  const t = Math.max(geometry.t0, Math.min(geometry.end, geometry.invert(px)));
  const mains = geometry.lanes.filter(lane => lane.kind === 'main');
  if (!options.laneOnly) {
    if (geometry.head && Math.abs(px - geometry.head.px) <= 9 && Math.abs(py - geometry.presentY) <= 9) return {kind: 'head', t, y: geometry.presentY};
    for (const node of geometry.nodes) {
      if (Math.abs(px - node.px) <= node.w / 2 + 4 && Math.abs(py - geometry.presentY) <= node.h / 2 + 4) return {...node, kind: 'node'};
    }
    for (const lane of geometry.lanes.filter(item => item.kind === 'race')) {
      if (Math.abs(py - lane.y) <= lane.tolerance && t >= lane.t0 - 0.3 && t <= lane.t1 + 0.3) return {name: lane.name, t: Math.min(lane.t1, Math.max(lane.t0, t)), dead: lane.dead && t >= lane.t1 - 0.5, kind: 'lane', y: lane.y};
    }
  }
  if (options.laneOnly || py < geometry.presentY + 26) {
    const stretch = mains.find(item => t >= item.t0 - 0.3 && t <= item.t1 + 0.3) || mains.filter(item => item.t0 <= t).at(-1) || mains.at(-1);
    if (stretch) return {name: stretch.name, t: Math.min(stretch.t1, Math.max(stretch.t0, t)), dead: stretch.dead && t >= stretch.t1 - 0.5, kind: 'present', y: geometry.presentY};
  }
  return null;
}

function updateCursor() {
  const svg = $('map');
  const kind = run?.drag && !run.scan ? 'grabbing' : spaceHeld ? 'scrub' : run?.hover?.kind === 'head' && view?.mode === 'replay' ? 'grab' : ['node', 'lane'].includes(run?.hover?.kind) ? 'point' : '';
  for (const cls of ['scrub', 'point', 'grab', 'grabbing']) svg.classList.toggle(cls, cls === kind);
}

let hoverSeq = 0;
$('map').addEventListener('mousemove', event => {
  if (!run || !view) return;
  if (run.scan) { if (!spaceHeld) return; endScanQuiet(); }
  if (run.drag) { dragHead(event); return; }
  const target = hitTest(event.clientX, event.clientY);
  run.hover = target;
  renderCursor();
  updateCursor();
  if (run.peek?.pinned) return;
  if (!spaceHeld) return;
  spaceMoved = true;
  hintUsed();
  const seq = ++hoverSeq;
  peekAt(target, false, () => seq === hoverSeq);
});
$('map').addEventListener('mouseleave', () => {
  if (!run || run.scan) return;
  run.hover = null;
  renderCursor();
  updateCursor();
  hoverSeq++;
  if (!run.peek?.pinned && !run.drag) clearPeek();
});
$('map').addEventListener('mousedown', event => {
  if (!run || !view || event.target.closest('[data-slot], [data-branch], [data-fork]')) return;
  const target = hitTest(event.clientX, event.clientY);
  if (target?.kind === 'head' && view.mode === 'replay') {
    event.preventDefault();
    endScanQuiet();
    run.drag = {elapsed: view.elapsed, released: false};
    updateCursor();
  }
});
window.addEventListener('mousemove', event => { if (run?.drag && !run.scan && event.target !== $('map')) dragHead(event); });
window.addEventListener('mouseup', () => {
  if (!run?.drag || run.scan) return;
  run.drag.released = true;
  queueSeek(run.drag.elapsed);
  updateCursor();
});
$('map').addEventListener('click', event => {
  if (!run || !view) return;
  if (run.dragged) { run.dragged = false; return; }
  endScanQuiet();
  if (activateMapControl(event.target)) return;
  const target = hitTest(event.clientX, event.clientY);
  if (!target || target.kind === 'head') { clearPeek(); return; }
  if (run.peek?.pinned && run.peek.name === target.name && !target.slot && Math.abs(run.peek.elapsed - target.t) < 0.2) { clearPeek(); return; }
  run.peek = null;
  peekAt(target, true);
});
$('map').addEventListener('keydown', event => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  activateMapControl(event.target);
});

function activateMapControl(element) {
  const control = element.closest('[data-slot], [data-branch], [data-fork]');
  if (!control || !run?.graph) return false;
  if (control.dataset.slot) selectSlot(control.dataset.slot);
  else if (control.dataset.branch) selectBranch(run.graph.branches.find(item => item.name === control.dataset.branch));
  else { forkFocus = Number(control.dataset.fork); setDockTab('futures'); }
  return true;
}


// Dragging the playhead along the lane seeks the replay to the surviving history at that x.
// Dragging the playhead along the line seeks the replay to that moment.
async function dragHead(event) {
  const target = hitTest(event.clientX, event.clientY, {laneOnly: true});
  if (!target) return;
  run.dragged = true;
  run.dragT = target.t;
  run.hover = target;
  run.drag.elapsed = target.t;
  queueSeek(target.t);
  renderCursor();
  const seq = ++hoverSeq;
  await peekAt(target, false, () => seq === hoverSeq && Boolean(run.drag));
}

function renderCursor() {
  const svg = $('map');
  const geometry = run?.geometry;
  const seen = new Set();
  // The selection sticks: a lime line at the pinned moment, behind everything else.
  const pinned = run?.peek?.pinned && typeof run.peek.elapsed === 'number' ? run.peek : null;
  if (geometry && pinned) {
    const node = pinned.slot ? geometry.nodes.find(item => item.slot === pinned.slot) : null;
    const px = node ? node.px : geometry.X(pinned.elapsed);
    const line = upsert(svg, 'pinned-line', 'line', {x1: px, x2: px, y1: 0, y2: svg.clientHeight, class: 'cursor on'}, seen);
    if (svg.firstChild !== line) svg.prepend(line);
  } else document.getElementById('pinned-line')?.remove();
  const target = run?.hover;
  if (geometry && target && target.kind !== 'node' && target.kind !== 'head') {
    const px = geometry.X(target.t);
    upsert(svg, 'cursor', 'line', {x1: px, x2: px, y1: 0, y2: svg.clientHeight, class: `cursor${spaceHeld || run.drag || run.scan ? ' on' : ''}`}, seen);
    upsert(svg, 'cursor-dot', 'circle', {cx: px, cy: target.y, r: 3.5, class: 'cursor-dot'}, seen);
  } else {
    for (const id of ['cursor', 'cursor-dot']) document.getElementById(id)?.remove();
  }
}

// ---------------------------------------------------------------- the dock

const dockTabs = [...document.querySelectorAll('[data-tab]')];
function setDockTab(tab) { dockTab = tab; renderDock(); }
dockTabs.forEach((button, index) => {
  button.onclick = () => setDockTab(button.dataset.tab);
  button.onkeydown = event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? dockTabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + dockTabs.length) % dockTabs.length;
    setDockTab(dockTabs[next].dataset.tab);
    dockTabs[next].focus();
  };
});

function branchLabel(branch) {
  const verse = verseOf(branch.name);
  if (verse?.trait) return verse.trait;
  if (branch.experiment?.steps?.length) return branch.experiment.steps.map(step => `${SHORT[step.action] || step.action} ${step.frames}f`).join(' → ');
  if (branch.event?.type === 'multiverse') return SHORT[CANDIDATES[branch.index]] || verseName(branch.name);
  return branch.tail ? 'previous attempt' : 'rewound attempt';
}
function branchOutcome(branch) {
  const events = played();
  const isVerse = Boolean(verseOf(branch.name));
  if (events.some(event => event.type === 'promote' && event.sandbox === branch.name)) return branch.tail && branch.dead ? 'was canon · later collapsed' : 'canon';
  if (events.some(event => ['stage_clear', 'clear'].includes(event.type) && event.sandbox === branch.name)) return 'cleared the stage';
  if (branch.dead) return isVerse ? 'collapsed' : 'died';
  if (branch.role === 'candidate') return 'trying';
  return isVerse ? 'collapsed' : 'discarded';
}
function replaceContent(id, html) {
  const node = $(id);
  // Avoid resetting focused controls and open disclosures on every state tick.
  if (node.dataset.content !== html) {
    const open = [...node.querySelectorAll('details[open]')].map(detail => detail.querySelector('summary')?.textContent);
    node.innerHTML = html;
    node.dataset.content = html;
    for (const detail of node.querySelectorAll('details')) if (open.includes(detail.querySelector('summary')?.textContent)) detail.open = true;
  }
}
function row(label, value) {
  return `<div class="dock-row"><span>${esc(label)}</span><b>${esc(value)}</b></div>`;
}

function renderDock() {
  if (!run || !view) return;
  for (const button of dockTabs) {
    const selected = button.dataset.tab === dockTab;
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
    $(`dock-${button.dataset.tab}`).hidden = !selected;
  }
  if (dockTab === 'calls') { renderOps(); return; }
  if (dockTab === 'multiverse') { renderMultiverse(); return; }
  if (dockTab === 'moment') { renderMoment(); return; }
  renderFutures();
}

function renderMoment() {
  const peek = run.peek?.pinned ? run.peek : null;
  const graph = run.graph;
  if (peek?.slot) {
    const node = graph?.nodes.get(peek.slot);
    const live = view.mode !== 'replay' && view.busy;
    const frozen = live && timelineOf(peek.slot)?.role === 'checkpoint';
    const forksHere = graph ? graph.forks.filter(fork => fork.parent === peek.slot).length : 0;
    const rewindsHere = played().filter(event => event.type === 'rewind' && event.parent === peek.slot).length;
    replaceContent('dock-moment', `<p class="dock-kicker">${frozen ? 'frozen copy' : live ? 'old snapshot' : 'recorded checkpoint'} · ${esc(short(peek.slot))}</p>
      <h3>x ${esc(peek.x)} · ${clock(peek.elapsed)}</h3>
      ${row('copied from', short(node?.on) || '—')}
      ${row('status', frozen ? 'still frozen' : live ? 'evicted · picture only' : 'recorded')}
      ${forksHere ? row('forks from here', String(forksHere)) : ''}
      ${rewindsHere ? row('rewinds to here', String(rewindsHere)) : ''}
      <p class="dock-note">${frozen ? 'a paused copy of the whole machine: emulator, game, jev’s controller. rewind to it from the popover.' : live ? 'this copy was thrown away to make room. only the picture is left.' : 'a copy of the whole machine, taken between two jev calls.'}</p>`);
    return;
  }
  if (peek) {
    const branch = graph?.branches.find(item => item.name === peek.name);
    if (branch) {
      const outcome = branchOutcome(branch);
      replaceContent('dock-moment', `<p class="dock-kicker ${outcome === 'died' ? 'dead' : ''}">${esc(short(branch.name))} · ${esc(outcome)}</p>
        <h3>${esc(branchLabel(branch))}</h3>
        ${row('branched from', short(branch.slot) || 'start')}
        ${row('reached', `x ${branch.x1}`)}
        ${row('this frame', `x ${peek.x} · ${clock(peek.elapsed)}`)}
        <p class="dock-note">${branch.experiment ? 'a prepared action sequence. no model calls during the sequence; jev takes over afterwards.' : branch.event?.type === 'multiverse' ? 'one of the forks from that frozen copy, forced down its own first move.' : 'an earlier attempt from this run.'}</p>`);
      return;
    }
    replaceContent('dock-moment', `<p class="dock-kicker">${peek.dead ? 'a future that died' : 'a moment'}</p>
      <h3>${clock(peek.elapsed)} · x ${esc(peek.x)}</h3>
      ${row('frame', String(peek.frame))}
      ${row('machine', esc(short(peek.name)))}
      <p class="dock-note">← → step through it.</p>`);
    return;
  }
  const item = timelineOf(playedTrunk(played()));
  const action = item?.action;
  const probabilities = Object.entries(item?.probabilities || {}).sort((a, b) => b[1] - a[1]);
  replaceContent('dock-moment', `<p class="dock-kicker">${item?.forced ? 'host sequence' : 'last jev decision'}${view.mode === 'replay' ? ' · recorded' : ''}</p>
    <h3>${esc(LABEL[action] || action || 'waiting for a decision')}</h3>
    ${row('status', view.paused ? 'paused' : item?.phase || view.status)}
    ${row('api call', item?.forced ? 'none during sequence' : typeof item?.latency_ms === 'number' ? `${(item.latency_ms / 1000).toFixed(2)} s` : '—')}
    ${row('position', typeof item?.x_pos === 'number' ? `x ${item.x_pos}` : '—')}
    <details><summary>model response</summary><pre>${esc(probabilities.length ? probabilities.map(([move, p]) => `${(LABEL[move] || move).padEnd(20)} ${Math.round(p * 100)}%`).join('\n') : 'no probabilities recorded for this decision.')}</pre><p class="dock-note">preferences between moves, not survival odds.</p></details>
    <details><summary>what jev sees</summary><p class="dock-note">position, movement, nearby terrain, enemies, collectibles and recent attempts. structured game memory, every eight frames.</p></details>`);
}

function renderFutures() {
  const graph = run.graph;
  if (!graph) return;
  const races = [...graph.forks].reverse();
  const race = races.find(event => event.t === forkFocus) || races[0];
  const branches = race ? race.children.map(name => graph.segments.get(name)).filter(Boolean) : [];
  const selected = run.peek?.pinned ? (run.peek.slot || run.peek.name) : null;
  const rows = branches.map(branch => `<button class="future-card ${branchOutcome(branch) === 'survived' ? 'survived' : ''} ${selected === branch.name ? 'selected' : ''}" data-select-branch="${esc(branch.name)}">
    <span class="future-letter">${verseOf(branch.name)?.no ?? letter(branch.index)}</span><span><b>${esc(branchLabel(branch))}</b><small>${esc(branchOutcome(branch))}</small></span><span>↗</span></button>`).join('');
  const checkpoints = [...graph.nodes.values()].reverse().map(node => {
    const retained = view.mode !== 'replay' && view.busy && timelineOf(node.slot)?.role === 'checkpoint';
    return `<button class="history-copy ${selected === node.slot ? 'selected' : ''}" data-select-slot="${esc(node.slot)}"><span>${esc(short(node.slot))} · x ${node.x}</span><span>${retained ? 'frozen' : 'recorded'} · ${clock(node.elapsed)}</span></button>`;
  }).join('');
  const attempts = [...graph.branches].reverse().map(branch => `<button class="history-copy ${selected === branch.name ? 'selected' : ''}" data-select-branch="${esc(branch.name)}"><span>${esc(short(branch.name))}</span><span>${esc(branchOutcome(branch))} ↗</span></button>`).join('');
  replaceContent('dock-futures', `${races.length > 1 ? `<div class="dock-race">fork ${menuHtml('dock-race', races.map(event => ({value: event.t, label: `${clock(event.elapsed)} · ${short(event.parent)}`})), race?.t ?? '')}</div>` : ''}
    <p class="dock-kicker">${race ? `${branches.length} futures · from ${esc(short(race.parent))}` : 'no fork yet'}</p>${rows}
    ${attempts ? `<details><summary>all alternate futures (${graph.branches.length})</summary>${attempts}</details>` : ''}
    <details${race ? '' : ' open'}><summary>checkpoints (${graph.nodes.size})</summary>${checkpoints || '<p class="dock-note">the first copy will appear here.</p>'}</details>
    <p class="dock-note">a checkpoint keeps a paused copy of the whole machine. a fork tries different futures from that same copy. the survivor continues on the lime line.</p>`);
}
$('dock-futures').addEventListener('menu-change', event => {
  if (event.target.id !== 'dock-race') return;
  forkFocus = Number(event.detail.value);
  renderFutures();
});
$('dock-futures').addEventListener('click', event => {
  const target = event.target.closest('[data-select-branch], [data-select-slot]');
  if (!target || !run?.graph) return;
  if (target.dataset.selectSlot) selectSlot(target.dataset.selectSlot);
  else selectBranch(run.graph.branches.find(item => item.name === target.dataset.selectBranch) || run.graph.segments.get(target.dataset.selectBranch));
});

// ---------------------------------------------------------------- the timeline

function upsert(parent, id, tag, attrs, seen) {
  let node = document.getElementById(id);
  if (!node) {
    node = document.createElementNS(SVG, tag);
    node.id = id;
    if (tag === 'image') {
      // A picture that is not there yet (the frame-zero copy, briefly) is retried, not
      // abandoned: an error hides it and asks again a little later.
      node.addEventListener('error', () => {
        node.setAttribute('opacity', '0');
        const tries = Number(node.dataset.tries || 0);
        if (tries >= 6) return;
        node.dataset.tries = String(tries + 1);
        setTimeout(() => { if (node.isConnected) node.setAttribute('href', `${node.dataset.src}&r=${tries + 1}`); }, 1500 * (tries + 1));
      });
      node.addEventListener('load', () => node.setAttribute('opacity', '1'));
    }
    parent.append(node);
  } else if (node.parentElement !== parent) {
    parent.append(node);
  }
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'text') { if (node.textContent !== String(value)) node.textContent = value; }
    else if (key === 'href') { if (node.dataset.src !== value) { node.dataset.src = value; node.dataset.tries = '0'; node.setAttribute('href', value); } }
    else if (key === 'class') {
      const keep = ['branching', 'landed', 'hot'].filter(cls => node.classList.contains(cls));
      const next = `${value} ${keep.join(' ')}`.trim();
      if (node.getAttribute('class') !== next) node.setAttribute('class', next);
    }
    else if (node.getAttribute(key) !== String(value)) node.setAttribute(key, value);
  }
  seen.add(id);
  return node;
}

// A replay knows its whole schedule, so a world can be fitted to the line from the start.
async function loadSchedule(owner) {
  try {
    const schedule = await (await fetch('/api/schedule')).json();
    if (run === owner) { run.schedule = schedule; run.canon = buildCanon(schedule); renderPlayback(true); renderMap(); }
  } catch {}
}

// ---------------------------------------------------------------- skip deaths: the verse that survived
//
// A recording knows how it ends, so it knows which machines the present descends from.
// The surviving path is each of those machines from the moment it starts moving to the
// checkpoint its successor was branched from. A branch resumes on the exact frame of its
// checkpoint, so cutting from one to the next shows no jump: mario simply does not die.
// Times here are the replay's own clock ("full"); the cut has a shorter clock of its own.

function buildCanon(schedule) {
  const events = schedule?.events || [];
  const anchors = schedule?.anchors || {};
  const copies = new Map(), opened = new Map(), ended = new Map(), worlds = [];
  let trunk = null, worldT0 = 0;
  for (const event of events) {
    const t = event.elapsed;
    switch (event.type) {
      case 'created': trunk = event.sandbox; opened.set(trunk, {slot: null, at: t}); break;
      case 'checkpoint': copies.set(event.child, {t, on: event.sandbox}); break;
      case 'rewind': trunk = event.sandbox; opened.set(trunk, {slot: event.parent, at: t}); break;
      case 'multiverse': for (const child of event.children) opened.set(child, {slot: event.parent, at: t, race: event.children}); break;
      case 'promote': trunk = event.sandbox; break;
      case 'death': ended.set(event.sandbox, t); break;
      case 'stage_clear': case 'clear': if (trunk) worlds.push({t0: worldT0, final: event.sandbox || trunk, end: t}); trunk = null; break;
      case 'stage_started': worldT0 = t; break;
      default: break;
    }
  }
  if (trunk) worlds.push({t0: worldT0, final: trunk, end: ended.get(trunk) ?? schedule.end ?? 0});
  // A fork or a rewind holds the picture still for a beat before the machine moves.
  const moving = (name, at) => {
    const list = (anchors[name] || []).filter(anchor => anchor[0] >= at - 0.001);
    if (!list.length) return at;
    let index = 0;
    while (index + 1 < list.length && list[index + 1][1] === list[0][1]) index++;
    return list[index][0];
  };
  const segments = [];
  for (const world of worlds) {
    const chain = [];
    let name = world.final, to = world.end;
    for (let guard = 0; name && guard < 500; guard++) {
      const how = opened.get(name);
      if (!how) break;
      const from = Math.min(moving(name, how.at), to);
      chain.unshift({name, from, to, race: how.race || null, world: world.t0});
      const copy = how.slot ? copies.get(how.slot) : null;
      if (!copy) break;
      to = copy.t;
      name = copy.on;
    }
    segments.push(...chain.filter(segment => segment.to - segment.from > 0.01));
  }
  // Between worlds nothing is cut: the flag, the stamp and the new machine all stay.
  for (let index = 0; index + 1 < segments.length; index++) {
    if (segments[index].world !== segments[index + 1].world) segments[index].to = segments[index + 1].from;
  }
  let c = 0;
  const folds = [];
  segments.forEach((segment, index) => {
    segment.c0 = c;
    c += segment.to - segment.from;
    segment.c1 = c;
    const next = segments[index + 1];
    segment.next = next && next.from - segment.to > 0.05 ? next : null;
    if (!segment.next) return;
    // what the fold removes: the deaths of the present, and every verse of a race nobody survived
    let deaths = 0, racers = 0;
    for (const event of events) {
      if (event.elapsed <= segment.to || event.elapsed > next.from) continue;
      if (event.type === 'death') deaths++;
      else if (event.type === 'multiverse') racers = event.children.length;
      else if (event.type === 'race_failed') deaths += racers;
    }
    folds.push({at: segment.to, c: segment.c1, deaths, world: segment.world});
  });
  const canon = {
    segments, folds, total: c,
    segmentAt: t => segments.find(segment => t >= segment.from - 0.001 && t <= segment.to + 0.001) || null,
    nextStart: t => segments.find(segment => segment.from > t)?.from ?? null,
    toCanon: t => {
      for (const segment of segments) {
        if (t < segment.from) return segment.c0;
        if (t <= segment.to) return segment.c0 + (t - segment.from);
      }
      return c;
    },
    toFull: value => {
      const segment = segments.find(item => value <= item.c1) || segments.at(-1);
      return segment ? Math.min(segment.to, Math.max(segment.from, segment.from + (value - segment.c0))) : 0;
    },
  };
  return canon;
}
const canonOn = () => skipDeaths && view?.mode === 'replay' && Boolean(run?.canon?.segments.length);

// The verse that is known to win the race on screen, while the cut is playing.
function canonWinner() {
  if (!canonOn()) return null;
  const segment = run.canon.segmentAt(run.drag ? run.drag.elapsed : view.elapsed || 0);
  return segment?.race ? segment.name : null;
}

// Crossing a fold: jump from the checkpoint to where the surviving verse starts moving.
const HOP_LEAD = 0.1;
function canonHop() {
  if (!canonOn() || run.drag || run.scan) return;
  const t = view.elapsed || 0;
  const segment = run.canon.segmentAt(t);
  let target = null;
  if (!segment) target = run.canon.nextStart(t);
  else if (segment.next && !view.paused && t >= segment.to - HOP_LEAD * (view.speed || 1)) target = segment.next.from;
  if (target === null || Math.abs(target - t) < 0.05) return;
  if (run.hopTarget === target && performance.now() - run.hopAt < 1500) return;
  run.hopTarget = target;
  run.hopAt = performance.now();
  wipe();
  queueSeek(target);
}

// The cut mark: a lime wipe across the game, gone in under a fifth of a second.
function wipe() {
  const node = $('wipe');
  node.hidden = false;
  node.classList.remove('go');
  void node.offsetWidth;
  node.classList.add('go');
  clearTimeout(wipe.timer);
  wipe.timer = setTimeout(() => { node.hidden = true; }, 260);
}
function worldSpan(t0) {
  const schedule = run?.schedule;
  if (!schedule?.events) return null;
  const next = schedule.events.find(event => event.type === 'stage_started' && event.elapsed > t0 + 0.001);
  const end = next ? next.elapsed : (schedule.end ?? view.duration ?? 0);
  return end > t0 ? end - t0 : null;
}

// Each world is its own timeline: the line starts anew when a stage begins.
function worldStart(events) {
  const start = [...events].reverse().find(event => event.type === 'stage_started');
  return start ? start.elapsed : 0;
}

function renderMap() {
  if (!view || !run) return;
  const svg = $('map');
  applyZoom();
  const all = played();
  const trunk = playedTrunk(all);
  const t0 = worldStart(all);
  const events = all.filter(event => event.elapsed >= t0 - 0.001);
  run.graph = MarioTimeline.build(events, view.timelines, trunk);   // ancestry for the dock
  const timelines = new Map(view.timelines.map(item => [item.name, item]));
  const seen = new Set(['cursor', 'cursor-dot', 'pinned-line']);
  const width = svg.clientWidth || 800, height = svg.clientHeight || 200;
  // The clock sits at the end of the line, so the line stops short of it.
  const left = 40, right = Math.max(40, ($('tp-clock').offsetWidth || 0) + 30), presentY = 40;
  const now = view.elapsed || 0;
  if (run.stageT0 !== t0) { run.stageT0 = t0; run.span = null; run.follow = true; $('map-scroll').scrollLeft = 0; }

  // Time runs left to right from the start of this world. A replay fits the world to the
  // line when its schedule is known; otherwise the span grows in 1.5x steps, so the line
  // only re-lays itself out a few times.
  const fit = view.mode === 'replay' ? worldSpan(t0) : null;
  let span = fit || run.span || 90;
  if (!fit) { while (now - t0 > span * 0.88) span *= 1.5; run.span = span; }
  const X = t => left + (t - t0) / span * Math.max(1, width - left - right);
  const invert = px => t0 + (px - left) / Math.max(1, width - left - right) * span;
  if (canonOn()) { renderCanonMap({svg, seen, width, height, left, right, presentY, now, timelines, events, trunk, t0}); return; }
  const geometry = {X, invert, presentY, span, t0, end: t0 + span, nodes: [], lanes: [], head: null};

  // What happened, when: stretches of machines running (main line or a race lane), the
  // copies taken, the rewinds back to them, the forks, the stage boundaries.
  const stretches = [], open = new Map(), nodes = new Map(), forks = [], stages = [], opened = new Map();
  const close = (name, t, outcome) => { const stretch = open.get(name); if (!stretch) return; stretch.t1 = t; stretch.outcome = outcome; open.delete(name); };
  const start = (name, t, kind, extra = {}) => { const stretch = {name, t0: t, t1: null, kind, outcome: 'alive', ...extra}; stretches.push(stretch); open.set(name, stretch); return stretch; };
  const deadRole = name => timelines.get(name)?.role === 'dead' || timelines.get(name)?.phase === 'dead';
  for (const event of events) {
    const t = event.elapsed;
    switch (event.type) {
      case 'created': start(event.sandbox, t, 'main'); opened.set(event.sandbox, {slot: null}); break;
      case 'checkpoint': nodes.set(event.child, {slot: event.child, t, on: event.sandbox, x: event.x_pos, frame: event.frame, elapsed: t}); break;
      case 'death': close(event.sandbox, t, 'dead'); break;
      case 'rewind': start(event.sandbox, t, 'main'); opened.set(event.sandbox, {slot: event.parent}); break;
      case 'multiverse': {
        const fork = {t, parent: event.parent, children: event.children, t1: null, winner: null};
        forks.push(fork);
        event.children.forEach((child, index) => { start(child, t, 'race', {fork, index}); opened.set(child, {slot: event.parent}); });
        break;
      }
      case 'promote': {
        const fork = forks.at(-1);
        if (fork) { fork.t1 = t; fork.winner = event.sandbox; }
        for (const child of fork?.children || []) close(child, t, child === event.sandbox ? 'promoted' : deadRole(child) ? 'dead' : 'lost');
        start(event.sandbox, t, 'main');
        opened.set(event.sandbox, {slot: fork?.parent ?? null});
        break;
      }
      case 'race_failed': {
        const fork = forks.at(-1);
        if (fork) fork.t1 = t;
        for (const child of fork?.children || []) close(child, t, deadRole(child) ? 'dead' : 'lost');
        break;
      }
      case 'stage_clear': case 'clear': close(event.sandbox, t, 'cleared'); break;
      case 'stage_started': stages.push({t, stage: event.stage}); break;
      case 'failed': case 'stopped': case 'cleanup_failed': for (const name of [...open.keys()]) close(name, t, 'halted'); break;
      default: break;
    }
  }
  for (const stretch of open.values()) stretch.t1 = Math.max(now, stretch.t0);
  for (const fork of forks) if (fork.t1 === null) fork.t1 = now;

  // Which parts of the line survive: the present, and each ancestor up to the copy the
  // present descends from. Everything past that copy on the ancestor was thrown away.
  const surviving = new Map();
  const chase = (name, cut) => {
    let current = name, limit = cut;
    while (current && !surviving.has(current)) {
      surviving.set(current, limit);
      const how = opened.get(current);
      const node = how?.slot ? nodes.get(how.slot) : null;
      if (!node) break;
      limit = node.t;
      current = node.on;
    }
  };
  chase(trunk, Infinity);
  for (const stretch of stretches) if (stretch.outcome === 'cleared') chase(stretch.name, stretch.t1);

  // Axis and ticks.
  upsert(svg, 'axis', 'line', {x1: left, x2: width - right, y1: presentY, y2: presentY, class: 'baseline'}, seen);
  stages.forEach((mark, index) => {
    upsert(svg, `stage-${index}`, 'line', {x1: X(mark.t), x2: X(mark.t), y1: 4, y2: height - 26, class: 'stage-mark'}, seen);
    upsert(svg, `stage-${index}-label`, 'text', {x: X(mark.t) + 5, y: 14, class: 'stage-label', text: G().goal(mark.stage)}, seen);
  });

  // The line: every machine that was the present, coloured by whether that stretch of it
  // is still part of the story (lime) or was thrown away (red where it died, grey otherwise).
  stretches.filter(stretch => stretch.kind === 'main').forEach(stretch => {
    const cut = surviving.get(stretch.name);
    const parts = [];
    if (cut === undefined || cut <= stretch.t0) parts.push([stretch.t0, stretch.t1, false]);
    else if (cut >= stretch.t1) parts.push([stretch.t0, stretch.t1, true]);
    else { parts.push([stretch.t0, cut, true]); parts.push([cut, stretch.t1, false]); }
    parts.forEach(([a, b, alive], index) => {
      const live = alive && stretch.name === trunk && stretch.outcome === 'alive';
      const tone = alive ? (live ? 'trunk' : 'history') : stretch.outcome === 'dead' ? 'dead' : 'discarded';
      upsert(svg, `stretch-${stretch.name}-${index}`, 'line', {x1: X(a), x2: X(b), y1: presentY, y2: presentY, class: `lane ${tone}`, 'data-name': stretch.name}, seen);
    });
    if (stretch.outcome === 'dead') cross(svg, `stretch-${stretch.name}-x`, X(stretch.t1), presentY, seen, false);
    if (stretch.outcome === 'cleared') upsert(svg, `stretch-${stretch.name}-clear`, 'rect', {x: X(stretch.t1) - 5, y: presentY - 5, width: 10, height: 10, class: 'cleared-mark'}, seen);
    geometry.lanes.push({name: stretch.name, t0: stretch.t0, t1: stretch.t1, y: presentY, kind: 'main', dead: stretch.outcome === 'dead', tolerance: 10});
  });

  // Copies, at the moment they were taken. Retained copies are always thumbnails; evicted
  // ones are smaller thumbnails where they fit and ticks where they do not.
  const candidates = [...nodes.values()].map(node => ({...node, retained: timelines.get(node.slot)?.role === 'checkpoint'}));
  const placed = [];
  const fits = (px, w) => placed.every(other => Math.abs(other.px - px) >= (other.w + w) / 2 + 6);
  const sizes = new Map();
  for (const node of candidates.filter(item => item.retained).reverse()) {
    const px = X(node.t);
    const [big, small] = G().tall ? [28, 20] : [46, 30];        // a tall game's copies are tall thumbnails
    const w = fits(px, big) ? big : fits(px, small) ? small : 0;
    if (w) { sizes.set(node.slot, {w, h: G().tall ? Math.round(w * 16 / 9) : w - 2}); placed.push({px, w}); }
    else sizes.set(node.slot, {w: 8, h: 12, tick: true});
  }
  if (markers) for (const node of candidates.filter(item => !item.retained)) sizes.set(node.slot, {w: 6, h: 10, tick: true, evicted: true});
  for (const node of candidates) {
    const size = sizes.get(node.slot);
    if (!size) continue;
    const x = X(node.t), {w, h} = size;
    const group = upsert(svg, `group-${node.slot}`, 'g', {
      class: `node${node.retained ? '' : ' evicted'}${size.tick ? ' tick' : ''}${run.peek?.slot === node.slot && run.peek.pinned ? ' pinned' : ''}`,
      tabindex: 0, role: 'button', 'data-slot': node.slot,
      'aria-label': `${short(node.slot)} · ${node.retained && view.mode !== 'replay' ? 'frozen copy' : 'checkpoint'} · ${clock(node.t)}`,
    }, seen);
    upsert(group, `node-${node.slot}`, 'rect', {x: x - w / 2, y: presentY - h / 2, width: w, height: h, class: 'node-border'}, seen);
    if (!size.tick) upsert(group, `img-${node.slot}`, 'image', {x: x - w / 2 + 2, y: presentY - h / 2 + 2, width: w - 4, height: h - 4, href: `/api/frame/${encodeURIComponent(node.slot)}?f=slot`, preserveAspectRatio: 'none', class: 'node-img'}, seen);
    geometry.nodes.push({...node, sandbox: node.on, px: x, w, h});
  }

  // Forks: the verses run side by side beneath the line for as long as the race lasted;
  // the survivor curves back up into the line, the others end where they ended.
  const latest = forks.at(-1);
  const laneGap = 14;
  stretches.filter(stretch => stretch.kind === 'race').forEach(stretch => {
    const active = stretch.outcome === 'alive';
    if (!markers && stretch.fork !== latest) return;
    if (!markers && !active && stretch.fork === latest && !run.winner) return;
    const y = presentY + (G().tall ? 38 : 24) + stretch.index * laneGap;
    const x0 = X(stretch.t0), xe = Math.max(x0 + 14, X(stretch.t1) - 14), x1 = X(stretch.t1);
    const item = timelines.get(stretch.name);
    const dead = stretch.outcome === 'dead' || (active && item?.phase === 'dead');
    const won = stretch.outcome === 'promoted' || (active && run.winner === stretch.name);
    const lost = stretch.outcome === 'lost' || (Boolean(run.winner) && active && !won);
    const tone = won ? 'winner' : dead ? 'dead' : active ? 'candidate' : lost ? 'loser' : 'discarded';
    const near = stretch.fork === latest;
    const d = `M${x0} ${presentY} C ${x0 + 8} ${presentY} ${x0 + 8} ${y} ${x0 + 16} ${y} L ${xe} ${y}` + (won && !active ? ` C ${xe + 8} ${y} ${xe + 8} ${presentY} ${x1} ${presentY}` : '');
    upsert(svg, `race-${stretch.name}`, 'path', {d, class: `lane ${tone}${near ? ' near' : ''}`, 'data-name': stretch.name}, seen);
    const hit = upsert(svg, `race-hit-${stretch.name}`, 'path', {d, class: 'branch-hit', tabindex: 0, role: 'button', 'data-branch': stretch.name, 'aria-label': `${verseTitle(stretch.name)} · ${won ? 'canon' : dead ? 'collapsed' : active ? 'trying' : 'collapsed'}`}, seen);
    hit.onfocus = () => { run.hot = {name: stretch.name}; renderHot(); };
    hit.onblur = () => { run.hot = null; renderHot(); };
    if (dead) cross(svg, `race-${stretch.name}-x`, xe, y, seen, !near);
    else if (active) upsert(svg, `race-${stretch.name}-head`, 'circle', {cx: xe, cy: y, r: won ? 5 : 4, class: `head ${won ? '' : 'candidate'}`}, seen);
    else if (!won) upsert(svg, `race-${stretch.name}-end`, 'line', {x1: xe, x2: xe, y1: y - 4, y2: y + 4, class: 'stub-end'}, seen);
    if (near) upsert(svg, `race-${stretch.name}-label`, 'text', {x: x0 + 22, y: y + 4, class: `lane-label ${tone}`, text: String(verseOf(stretch.name)?.no ?? letter(stretch.index))}, seen);
    geometry.lanes.push({name: stretch.name, t0: stretch.t0, t1: stretch.t1, y, kind: 'race', dead, tolerance: 6});
  });

  // The head: now, with the clock beside it. It never moves left on its own.
  if (trunk) {
    const shown = run.drag && typeof run.dragT === 'number' ? run.dragT : now;
    const hx = X(shown);
    const over = view.mode !== 'replay' && !['running', 'paused'].includes(view.status);
    upsert(svg, 'head-ring', 'circle', {cx: hx, cy: presentY, r: 6, class: `head-ring${view.paused || over ? ' still' : ''}`}, seen);
    upsert(svg, 'head', 'circle', {cx: hx, cy: presentY, r: 5, class: 'head'}, seen);
    geometry.head = {px: hx};
  }

  for (const node of [...svg.querySelectorAll('[id]')]) if (!seen.has(node.id) && !node.id.startsWith('fx-')) node.remove();
  run.geometry = geometry;
  followHead();
  renderCursor();
  renderHot();
  renderDock();
  if (run.peek?.pinned) renderPin();
}

// The timeline while deaths are skipped: only the surviving line, on its own shorter clock.
// Where a loop was folded away a small mark hangs under the line with what it removed.
function renderCanonMap({svg, seen, width, height, left, right, presentY, now, timelines, events, trunk, t0}) {
  const canon = run.canon;
  const mine = canon.segments.filter(segment => segment.world === t0);
  const segments = mine.length ? mine : canon.segments;
  const c0 = segments[0].c0, span = Math.max(0.5, segments.at(-1).c1 - c0);
  const usable = Math.max(1, width - left - right);
  const X = t => left + (canon.toCanon(t) - c0) / span * usable;
  const invert = px => canon.toFull(c0 + Math.max(0, Math.min(1, (px - left) / usable)) * span);
  const geometry = {X, invert, presentY, span, t0: segments[0].from, end: segments.at(-1).to, nodes: [], lanes: [], head: null, canon: true};
  const shown = run.drag && typeof run.dragT === 'number' ? run.dragT : now;
  const here = canon.toCanon(shown);

  upsert(svg, 'axis', 'line', {x1: left, x2: width - right, y1: presentY, y2: presentY, class: 'baseline'}, seen);
  const started = events.find(event => event.type === 'stage_started');
  if (started) {
    upsert(svg, 'stage-0', 'line', {x1: left, x2: left, y1: 4, y2: height - 26, class: 'stage-mark'}, seen);
    upsert(svg, 'stage-0-label', 'text', {x: left + 5, y: 14, class: 'stage-label', text: G().goal(started.stage)}, seen);
  }
  upsert(svg, 'canon-line', 'line', {x1: left, x2: X(shown), y1: presentY, y2: presentY, class: 'lane trunk'}, seen);
  for (const segment of segments) geometry.lanes.push({name: segment.name, t0: segment.from, t1: segment.to, y: presentY, kind: 'main', dead: false, tolerance: 10});

  // folds the head has passed
  canon.folds.filter(fold => fold.world === segments[0].world && fold.c <= here + 0.001).forEach((fold, index) => {
    const x = left + (fold.c - c0) / span * usable;
    upsert(svg, `fold-${index}-stem`, 'line', {x1: x, x2: x, y1: presentY + 3, y2: presentY + 28, class: 'fold-stem'}, seen);
    cross(svg, `fold-${index}`, x, presentY + 35, seen, false);
    if (fold.deaths > 1) upsert(svg, `fold-${index}-count`, 'text', {x: x + 9, y: presentY + 39, class: 'mark-count', text: `×${fold.deaths}`}, seen);
  });

  // copies taken on the surviving line
  const onPath = event => segments.some(segment => segment.name === event.sandbox && event.elapsed <= segment.to + 0.001);
  const candidates = events.filter(event => event.type === 'checkpoint' && onPath(event))
    .map(event => ({slot: event.child, t: event.elapsed, on: event.sandbox, x: event.x_pos, frame: event.frame, elapsed: event.elapsed, retained: timelines.get(event.child)?.role === 'checkpoint'}));
  const placed = [];
  const fits = (px, w) => placed.every(other => Math.abs(other.px - px) >= (other.w + w) / 2 + 6);
  const sizes = new Map();
  for (const node of candidates.filter(item => item.retained).reverse()) {
    const px = X(node.t);
    const [big, small] = G().tall ? [28, 20] : [46, 30];        // a tall game's copies are tall thumbnails
    const w = fits(px, big) ? big : fits(px, small) ? small : 0;
    if (w) { sizes.set(node.slot, {w, h: G().tall ? Math.round(w * 16 / 9) : w - 2}); placed.push({px, w}); }
    else sizes.set(node.slot, {w: 8, h: 12, tick: true});
  }
  if (markers) for (const node of candidates.filter(item => !item.retained)) sizes.set(node.slot, {w: 6, h: 10, tick: true});
  for (const node of candidates) {
    const size = sizes.get(node.slot);
    if (!size) continue;
    const x = X(node.t), {w, h} = size;
    const group = upsert(svg, `group-${node.slot}`, 'g', {
      class: `node${node.retained ? '' : ' evicted'}${size.tick ? ' tick' : ''}${run.peek?.slot === node.slot && run.peek.pinned ? ' pinned' : ''}`,
      tabindex: 0, role: 'button', 'data-slot': node.slot, 'aria-label': `${short(node.slot)} · checkpoint`,
    }, seen);
    upsert(group, `node-${node.slot}`, 'rect', {x: x - w / 2, y: presentY - h / 2, width: w, height: h, class: 'node-border'}, seen);
    if (!size.tick) upsert(group, `img-${node.slot}`, 'image', {x: x - w / 2 + 2, y: presentY - h / 2 + 2, width: w - 4, height: h - 4, href: `/api/frame/${encodeURIComponent(node.slot)}?f=slot`, preserveAspectRatio: 'none', class: 'node-img'}, seen);
    geometry.nodes.push({...node, sandbox: node.on, px: x, w, h});
  }

  if (trunk) {
    const hx = X(shown);
    upsert(svg, 'head-ring', 'circle', {cx: hx, cy: presentY, r: 6, class: `head-ring${view.paused ? ' still' : ''}`}, seen);
    upsert(svg, 'head', 'circle', {cx: hx, cy: presentY, r: 5, class: 'head'}, seen);
    geometry.head = {px: hx};
  }
  for (const node of [...svg.querySelectorAll('[id]')]) if (!seen.has(node.id) && !node.id.startsWith('fx-')) node.remove();
  run.geometry = geometry;
  followHead();
  renderCursor();
  renderHot();
  renderDock();
  if (run.peek?.pinned) renderPin();
}

function cross(svg, id, x, y, seen, faint) {
  const r = 4;
  upsert(svg, `${id}-a`, 'line', {x1: x - r, x2: x + r, y1: y - r, y2: y + r, class: `cross ${faint ? 'faint' : ''}`}, seen);
  upsert(svg, `${id}-b`, 'line', {x1: x - r, x2: x + r, y1: y + r, y2: y - r, class: `cross ${faint ? 'faint' : ''}`}, seen);
}

// ---------------------------------------------------------------- video export

// A recording becomes a video here: the page draws every frame (the same recorded frames,
// the same game clock and beats, the race as the 2x2 grid, the narration burned in) and the
// host encodes them with ffmpeg, as fast as the page can draw. Without ffmpeg the page
// records its own canvas in real time instead, which only works for the replay on screen.
let exporting = null;
// The video takes the game's shape: square for mario, vertical (1080 x 1920) for a tall game.
// The tall game fills the whole frame and the narration sits in a band over the ground, which
// hides no play; a fork's four tiles fit above that band.
const EXPORT_LAYOUTS = {
  mario: {w: 1200, h: 1200, gameX: 88, gameY: 64, gameW: 1024, gameH: 960, captionY: 1092, pad: 88},
  bird: {w: 1080, h: 1920, gameX: 0, gameY: 0, gameW: 1080, gameH: 1920, captionY: 1744, pad: 64, band: 1600, grid: {x: 94, y: 8, w: 892, h: 1580}},
};
let EXPORT = EXPORT_LAYOUTS.mario;
const exportGame = schedule => GAMES[schedule?.game] || GAMES.mario;

// How long each beat's effect runs in the video, in seconds of the game clock.
const FX = {checkpoint: 0.5, death: 0.55, rewind: 0.6, multiverse: 0.6, promote: 1.5, stage_clear: 2.2, clear: 2.2};
const PROMOTE_HOLD = 0.9;
const easeOut = x => 1 - Math.pow(1 - Math.min(1, Math.max(0, x)), 3);
const lerp = (from, to, k) => from + (to - from) * k;

function exportShow(schedule, t) {
  let trunk = null, race = null, promoted = null;
  for (const event of schedule.events) {
    if (event.elapsed > t) break;
    if (event.type === 'promote') { promoted = race ? {race, at: event.elapsed, winner: event.sandbox} : null; trunk = event.sandbox; race = null; }
    else if (['created', 'rewind'].includes(event.type)) { trunk = event.sandbox; race = null; promoted = null; }
    else if (event.type === 'multiverse') { race = event; promoted = null; }
    else if (['race_failed', 'stage_started'].includes(event.type)) { race = null; promoted = null; }
  }
  // The four stay up while the survivor is marked, then it grows back into the whole frame.
  if (promoted && t - promoted.at < FX.promote) return {mode: 'grid', names: promoted.race.children, race: promoted.race, promote: {winner: promoted.winner, dt: t - promoted.at}};
  return race ? {mode: 'grid', names: race.children, race} : {mode: 'single', names: trunk ? [trunk] : []};
}

// When each timeline died: the trunk's death, or a trial that ended dead. A trial's
// moment is where its own frames stop, since its result is journaled later.
function exportDeadAt(schedule) {
  const at = {};
  for (const event of schedule.events) {
    const died = event.type === 'death' || (event.type === 'experiment_result' && event.outcome === 'dead');
    if (!died || at[event.sandbox] !== undefined) continue;
    at[event.sandbox] = event.type === 'death' ? event.elapsed : (schedule.anchors[event.sandbox]?.at(-1)?.[0] ?? event.elapsed);
  }
  return at;
}

// True while any effect is moving, so the renderer draws every frame instead of holding one.
function exportFxActive(schedule, t) {
  const deadAt = schedule.deadAt ??= exportDeadAt(schedule);
  // Beats without an effect of their own still change the narration, which fades in.
  const spoken = ['created', 'retry_hypothesis', 'race_failed', 'paused', 'resumed', 'stage_started'];
  return schedule.events.some(event => t >= event.elapsed && t - event.elapsed < (FX[event.type] ?? (spoken.includes(event.type) ? 0.25 : 0)))
    || Object.values(deadAt).some(at => t >= at && t - at < FX.death);
}

function exportRecent(schedule, t, types, name) {
  let found = null;
  for (const event of schedule.events) {
    if (event.elapsed > t) break;
    if (types.includes(event.type) && (name === undefined || event.sandbox === name)) found = event;
  }
  return found && t - found.elapsed < FX[found.type] ? found : null;
}

function exportFrame(schedule, name, t) {
  const anchors = schedule.anchors[name];
  if (!anchors?.length) return null;
  let frame;
  if (t <= anchors[0][0]) frame = anchors[0][1];
  else {
    frame = anchors.at(-1)[1];
    for (let i = 0; i < anchors.length - 1; i++) {
      const [t0, f0] = anchors[i], [t1, f1] = anchors[i + 1];
      if (t <= t1) { frame = Math.round(f0 + (f1 - f0) * (t - t0) / Math.max(t1 - t0, 1e-6)); break; }
    }
  }
  const step = schedule.frame_step || 2;
  return Math.floor(frame / step) * step;
}

// The narration line at time t, as segments so the highlighted phrase can be drawn.
function exportCaption(schedule, t, shown = null) {
  const counts = {snapshots: 0, deaths: 0};
  let caption = [];
  for (const event of schedule.events) {
    if (event.elapsed > t) break;
    const before = caption;
    const quiet = shown && !shown(event);
    switch (event.type) {
      case 'created': caption = [[exportGame(schedule).machine]]; break;
      case 'checkpoint': counts.snapshots += 1; caption = counts.snapshots === 1 ? [['snapshot. microsandbox '], ['freezes a copy of the whole running machine.', 'ours']] : [['snapshot. another frozen copy of the whole machine.']]; break;
      case 'death': counts.deaths += 1; caption = [[`${exportGame(schedule).died} `], ['this verse ends here.', true]]; break;
      case 'retry_hypothesis': if (event.action) caption = [[`jev tried ${LABEL[event.action] || event.action} here. next time it picks something else.`]]; break;
      case 'rewind': caption = typeof event.branch_ms === 'number' ? [[event.manual ? 'you picked the snapshot. microsandbox branches it — ' : 'rewind. branch the frozen copy — '], [`${Math.round(event.branch_ms)} ms`, 'ours'], [` later ${exportGame(schedule).alive}.`]] : [['rewind. branch the frozen copy.']]; break;
      case 'multiverse': caption = [['split. '], ['four verses', true], [' from one moment.']]; break;
      case 'promote': caption = [['one verse made it. '], ['it is canon now.', true]]; break;
      case 'race_failed': caption = [['all of them collapsed. split again.']]; break;
      case 'paused': caption = [['paused. '], ['the whole machine is frozen mid-frame.', 'ours']]; break;
      case 'resumed': caption = [['resumed. the machine picks up exactly where it stopped.']]; break;
      case 'stage_clear': case 'clear': caption = [[`${exportGame(schedule).flag(event.stage || '')} `], [plural(counts.deaths, 'death'), true], [', zero game overs.']]; break;
      case 'stage_started': caption = [[`${exportGame(schedule).goal(event.stage || '')}. a new machine, new snapshots.`]]; break;
      default: break;
    }
    if (quiet) caption = before;
  }
  return caption;
}

function drawExportCaption(ctx, segments) {
  const {w, captionY} = EXPORT;
  ctx.font = '500 40px GeistPixelCircle, GeistPixelSquare, monospace';
  ctx.textBaseline = 'alphabetic';
  // Wrap words onto at most two lines, keeping each word's highlight.
  const words = [];
  for (const [text, em] of segments) for (const word of text.split(/(\s+)/)) if (word) words.push({word, em: em || false});
  const lines = [[]];
  let width = 0;
  for (const item of words) {
    const wordWidth = ctx.measureText(item.word).width;
    if (width + wordWidth > w - 160 && lines.at(-1).length && item.word.trim()) { lines.push([]); width = 0; }
    if (!lines.at(-1).length && !item.word.trim()) continue;
    lines.at(-1).push(item);
    width += wordWidth;
  }
  const lineHeight = 52;
  const top = captionY - ((lines.length - 1) * lineHeight) / 2;
  lines.forEach((line, index) => {
    const total = line.reduce((sum, item) => sum + ctx.measureText(item.word).width, 0);
    const y = top + index * lineHeight;
    // One continuous highlight per emphasised run (the words and the spaces between them).
    let x = (w - total) / 2;
    let runStart = null;
    line.forEach((item, position) => {
      const wordWidth = ctx.measureText(item.word).width;
      if (item.em && runStart === null) runStart = x;
      const next = line[position + 1];
      if (runStart !== null && (!next || next.em !== item.em)) {
        const end = x + wordWidth - (item.word.trim() ? 0 : wordWidth);
        ctx.fillStyle = item.em === 'ours' ? BRAND : '#d5f45c';
        ctx.fillRect(runStart - 4, y - 36, end - runStart + 8, 48);
        runStart = null;
      }
      x += wordWidth;
    });
    x = (w - total) / 2;
    for (const item of line) {
      ctx.fillStyle = item.em === 'ours' ? '#160a24' : item.em ? '#131708' : '#ffffff';
      ctx.fillText(item.word, x, y);
      x += ctx.measureText(item.word).width;
    }
  });
}

function drawExport(ctx, schedule, t, deaths, options = {}) {
  const {w, h, gameX, gameY, gameW, gameH, captionY, pad, band} = EXPORT;
  const grid = EXPORT.grid || {x: gameX, y: gameY, w: gameW, h: gameH};
  const LIME = '#d5f45c', RED = '#d97a85';
  ctx.filter = 'none';
  ctx.globalAlpha = 1;
  ctx.fillStyle = '#0e0e12';
  ctx.fillRect(0, 0, w, h);
  ctx.imageSmoothingEnabled = false;
  const show = exportShow(schedule, t);
  const deadAt = schedule.deadAt ??= exportDeadAt(schedule);
  const surviving = options.canon?.segmentAt(t);
  const known = show.mode === 'grid' && surviving?.race ? surviving.name : null;   // the cut already knows who lives
  const full = {x: gameX, y: gameY, w: gameW, h: gameH};
  const quad = index => ({x: grid.x + (index % 2) * (grid.w / 2 + 4), y: grid.y + Math.floor(index / 2) * (grid.h / 2 + 4), w: grid.w / 2 - 4, h: grid.h / 2 - 4});
  const mix = (from, to, k) => ({x: lerp(from.x, to.x, k), y: lerp(from.y, to.y, k), w: lerp(from.w, to.w, k), h: lerp(from.h, to.h, k)});
  let tiles;
  if (show.mode === 'grid') {
    // A fork splits the one frame into four; a promotion marks the survivor, then it grows back.
    const split = show.promote ? 1 : easeOut((t - show.race.elapsed) / FX.multiverse);
    const merge = show.promote ? easeOut((show.promote.dt - PROMOTE_HOLD) / (FX.promote - PROMOTE_HOLD)) : 0;
    tiles = show.names.map((name, index) => {
      const winner = name === (show.promote?.winner ?? known);
      const rect = show.promote ? (winner ? mix(quad(index), full, merge) : quad(index)) : mix(full, quad(index), split);
      return {name, ...rect, label: `verse ${letter(index)}${winner && show.promote ? ' · survived' : ''}`, winner, ghost: Boolean(known) && !winner, loser: Boolean(show.promote) && !winner, alpha: show.promote && !winner ? 1 - merge : 1, labelAlpha: show.promote ? 1 - merge : split};
    });
    tiles.sort((one, other) => Number(one.winner) - Number(other.winner));   // the survivor draws on top
  } else tiles = show.names.map(name => ({name, ...full, label: '', alpha: 1, labelAlpha: 0}));

  for (const tile of tiles) {
    const frame = exportFrame(schedule, tile.name, t);
    const image = frame === null ? null : frameCache.get(frameKey(tile.name, frame));
    if (frame !== null && options.preload !== false) preload(tile.name, frame, 30);
    const anchors = schedule.anchors[tile.name] || [];
    const diedAt = deadAt[tile.name] ?? (deaths.has(tile.name) && anchors.length ? anchors.at(-1)[0] : undefined);
    const dead = diedAt !== undefined && t >= diedAt && !tile.ghost;
    const since = dead ? t - diedAt : 0;
    const grey = dead ? Math.min(1, since / 0.4) : 0;
    const single = show.mode === 'single';
    const rewind = single ? exportRecent(schedule, t, ['rewind'], tile.name) : null;
    const rewound = rewind ? (t - rewind.elapsed) / FX.rewind : 1;
    // A death shakes the frame; a rewind judders it sideways like tape.
    const shake = dead && since < 0.35 ? Math.sin(since * 80) * 12 * (1 - since / 0.35) : 0;
    const judder = rewind ? [0, -0.02, 0.03, -0.01, 0, 0][Math.floor(rewound * 5)] * tile.w : 0;

    ctx.globalAlpha = tile.alpha;
    ctx.fillStyle = '#000';
    ctx.fillRect(tile.x, tile.y, tile.w, tile.h);
    ctx.save();
    ctx.beginPath();
    ctx.rect(tile.x, tile.y, tile.w, tile.h);
    ctx.clip();
    if (image) {
      ctx.filter = tile.ghost ? 'grayscale(1) brightness(0.3)' : tile.loser ? 'grayscale(1) brightness(0.35)' : dead ? `grayscale(${0.85 * grey}) brightness(${1 - 0.5 * grey})` : 'none';
      ctx.drawImage(image, tile.x + shake + judder, tile.y, tile.w, tile.h);
      ctx.filter = 'none';
    }
    if (dead) {
      // The red that closes in on a dead frame, and the flash at the moment it dies.
      const reach = Math.max(tile.w, tile.h);
      const vignette = ctx.createRadialGradient(tile.x + tile.w / 2, tile.y + tile.h / 2, reach * 0.3, tile.x + tile.w / 2, tile.y + tile.h / 2, reach * 0.75);
      vignette.addColorStop(0, 'rgba(217,122,133,0)');
      vignette.addColorStop(1, `rgba(217,122,133,${0.5 * grey})`);
      ctx.fillStyle = vignette;
      ctx.fillRect(tile.x, tile.y, tile.w, tile.h);
      if (since < 0.5) { ctx.fillStyle = `rgba(255,64,80,${0.55 * (1 - since / 0.5)})`; ctx.fillRect(tile.x, tile.y, tile.w, tile.h); }
    }
    const snapshot = single && !dead ? exportRecent(schedule, t, ['checkpoint'], tile.name) : null;   // a death outranks the copy taken just before it
    if (snapshot) {
      // The shutter: a wash in microsandbox's purple that snaps off as the copy is taken.
      const k = Math.min(1, (t - snapshot.elapsed) / 0.3);
      ctx.fillStyle = `rgba(192,132,252,${0.24 * (1 - k)})`;
      ctx.fillRect(tile.x, tile.y, tile.w, tile.h);
    }
    if (rewind) {
      const fade = rewound > 0.75 ? (1 - rewound) / 0.25 : 1;
      ctx.fillStyle = `rgba(0,0,0,${0.5 * fade})`;
      ctx.fillRect(tile.x, tile.y, tile.w, tile.h);
      ctx.fillStyle = `rgba(0,0,0,${0.35 * fade})`;
      const roll = (rewound * 120) % 10;
      for (let y = tile.y - roll; y < tile.y + tile.h; y += 10) ctx.fillRect(tile.x, y, tile.w, 4);
      ctx.globalAlpha = tile.alpha * fade;
      ctx.textAlign = 'center';
      ctx.font = '500 96px GeistPixelCircle, GeistPixelSquare, monospace';
      ctx.fillStyle = '#000';
      ctx.fillText('◀◀ rewind', tile.x + tile.w / 2 + 4, tile.y + tile.h / 2 + 4);
      ctx.fillStyle = LIME;
      ctx.fillText('◀◀ rewind', tile.x + tile.w / 2, tile.y + tile.h / 2);
      ctx.font = '500 26px GeistPixelSquare, monospace';
      ctx.fillStyle = '#fff';
      ctx.fillText(`${rewind.manual ? 'your snapshot' : 'branch the frozen copy'}${typeof rewind.branch_ms === 'number' ? ` · ${Math.round(rewind.branch_ms)} ms` : ''}`, tile.x + tile.w / 2, tile.y + tile.h / 2 + 52);
      ctx.textAlign = 'left';
      ctx.globalAlpha = tile.alpha;
    }
    ctx.restore();

    const shutter = snapshot ? 1 - Math.min(1, (t - snapshot.elapsed) / 0.3) : 0;
    ctx.strokeStyle = tile.winner ? LIME : dead ? RED : shutter ? `rgba(192,132,252,${shutter})` : 'rgba(255,255,255,.14)';
    ctx.lineWidth = tile.winner && show.promote ? 6 : tile.winner || dead ? 4 : shutter ? 8 : 2;
    ctx.strokeRect(tile.x + ctx.lineWidth / 2, tile.y + ctx.lineWidth / 2, tile.w - ctx.lineWidth, tile.h - ctx.lineWidth);

    if (snapshot && image) {
      // The copy itself: the frame shrinks away into the corner, framed in purple.
      const dt = t - snapshot.elapsed;
      const to = {x: tile.x + 28, y: tile.y + tile.h - tile.h * 0.16 - 28, w: tile.w * 0.16, h: tile.h * 0.16};
      const copy = mix(tile, to, easeOut(dt / 0.4));
      ctx.globalAlpha = tile.alpha * (dt < 0.38 ? 1 : Math.max(0, 1 - (dt - 0.38) / 0.12));
      ctx.drawImage(image, copy.x, copy.y, copy.w, copy.h);
      ctx.strokeStyle = BRAND;
      ctx.lineWidth = 4;
      ctx.strokeRect(copy.x, copy.y, copy.w, copy.h);
      ctx.globalAlpha = tile.alpha;
    }

    if (tile.label && tile.labelAlpha > 0.05) {
      ctx.globalAlpha = tile.alpha * tile.labelAlpha;
      ctx.font = '500 24px GeistPixelSquare, monospace';
      const text = `${tile.label}${dead ? ' ✕' : ''}`;
      const width = ctx.measureText(text).width + 20;
      ctx.fillStyle = 'rgba(14,14,18,.85)';
      ctx.fillRect(tile.x + 12, tile.y + 12, width, 36);
      ctx.fillStyle = tile.ghost ? 'rgba(255,255,255,.3)' : tile.winner ? LIME : dead ? RED : '#fff';
      ctx.fillText(text, tile.x + 22, tile.y + 38);
    }
    ctx.globalAlpha = 1;
  }
  if (typeof options.wipe === 'number') {                    // the cut mark, as on the page
    const edge = gameX + (options.wipe * 1.4 - 0.4) * gameW, band = gameW * 0.4;
    const from = Math.max(gameX, edge), to = Math.min(gameX + gameW, edge + band);
    if (to > from) {
      const gradient = ctx.createLinearGradient(edge, 0, edge + band, 0);
      gradient.addColorStop(0, 'rgba(213,244,92,0)');
      gradient.addColorStop(0.88, 'rgba(213,244,92,.26)');
      gradient.addColorStop(0.97, '#d5f45c');
      gradient.addColorStop(1, '#d5f45c');
      ctx.fillStyle = gradient;
      ctx.fillRect(from, gameY, to - from, gameH);
    }
  }
  if (band) {                                                // the narration's band, over the ground
    ctx.fillStyle = 'rgba(14,14,18,.92)';
    ctx.fillRect(0, band, w, h - band);
    ctx.fillStyle = 'rgba(255,255,255,.14)';
    ctx.fillRect(0, band, w, 2);
  }
  const cleared = exportRecent(schedule, t, ['stage_clear', 'clear']);
  if (cleared) {
    // The flag: the caption gives way to a stamp that lands, holds, and fades.
    const dt = t - cleared.elapsed;
    const scale = dt < 0.26 ? lerp(1.4, 1, easeOut(dt / 0.26)) : 1;
    ctx.globalAlpha = dt < 0.26 ? dt / 0.26 : dt > 1.8 ? Math.max(0, 1 - (dt - 1.8) / 0.4) : 1;
    ctx.save();
    ctx.translate(w / 2, captionY - 14);
    ctx.scale(scale, scale);
    ctx.textAlign = 'center';
    ctx.font = '500 84px GeistPixelCircle, GeistPixelSquare, monospace';
    ctx.fillStyle = LIME;
    ctx.fillText(`${exportGame(schedule).goal(cleared.stage || '')} clear`, 0, 28);
    ctx.restore();
    ctx.textAlign = 'left';
    ctx.globalAlpha = 1;
  } else {
    // Each new line of narration fades in, as it does on the page.
    const shown = options.canon ? event => Boolean(options.canon.segmentAt(event.elapsed)) : null;
    const caption = exportCaption(schedule, t, shown);
    const now = JSON.stringify(caption);
    let fade = 1;
    for (let step = 1; step <= 6; step++) {
      if (JSON.stringify(exportCaption(schedule, Math.max(0, t - step / 30), shown)) !== now) { fade = step / 7; break; }
    }
    ctx.globalAlpha = fade;
    drawExportCaption(ctx, caption);
    ctx.globalAlpha = 1;
  }

  ctx.font = '500 22px GeistPixelSquare, monospace';
  ctx.fillStyle = 'rgba(255,255,255,.52)';
  ctx.fillText(`${exportGame(schedule).hero} never dies`, pad, h - 34);
  const credit = 'built on ', brand = 'microsandbox';
  const right = w - pad - ctx.measureText(credit + brand).width;
  ctx.fillText(credit, right, h - 34);
  ctx.fillStyle = BRAND;
  ctx.fillText(brand, right + ctx.measureText(credit).width, h - 34);
}

async function exportVideo() {
  if (exporting || view?.mode !== 'replay') return;
  const schedule = await (await fetch('/api/schedule')).json();
  EXPORT = EXPORT_LAYOUTS[schedule.game] || EXPORT_LAYOUTS.mario;
  const mime = ['video/mp4;codecs=avc1.42E01E', 'video/mp4', 'video/webm;codecs=vp9', 'video/webm'].find(type => MediaRecorder.isTypeSupported(type));
  if (!mime) { $('error').textContent = 'this browser cannot record video'; $('error').hidden = false; return; }
  const deaths = new Set(schedule.events.filter(event => event.type === 'death').map(event => event.sandbox));
  const canvas = document.createElement('canvas');
  canvas.width = EXPORT.w;
  canvas.height = EXPORT.h;
  const ctx = canvas.getContext('2d');
  const stream = canvas.captureStream(30);
  const recorder = new MediaRecorder(stream, {mimeType: mime, videoBitsPerSecond: 8_000_000});
  const chunks = [];
  recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
  exporting = {cancelled: false};
  exportUi({fraction: 0, label: 'recording in real time'});
  renderPlayback(true);
  // Warm the cache for the opening second so the first frames are not black.
  const opening = exportShow(schedule, 0.1).names;
  await Promise.all(opening.flatMap(name => [0, 2, 4, 6, 8, 10, 12, 14].map(frame => fetchFrame(name, frame))));
  recorder.start(500);
  const started = performance.now();
  const end = schedule.end;
  await new Promise(resolve => {
    const tick = () => {
      if (exporting?.cancelled) { resolve(); return; }
      const t = Math.min(end, (performance.now() - started) / 1000);
      drawExport(ctx, schedule, t, deaths);
      exportUi({fraction: t / end, label: `recording ${clock(t)} / ${clock(end)}`});
      if (t >= end) { resolve(); return; }
      requestAnimationFrame(tick);
    };
    tick();
  });
  await new Promise(resolve => { recorder.onstop = resolve; recorder.stop(); });
  const cancelled = exporting?.cancelled;
  exporting = null;
  exportUi(null);
  renderPlayback(true);
  if (cancelled) return;
  const blob = new Blob(chunks, {type: mime});
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `${G().hero}-never-dies-${view.source || 'replay'}.${mime.startsWith('video/mp4') ? 'mp4' : 'webm'}`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 60_000);
}
document.addEventListener('click', event => { if (event.target.closest('.export-cancel') && exporting) exporting.cancelled = true; });

// The recording this page is about: the one being replayed, or the live run itself.
function recordingId() {
  if (!view) return null;
  if (view.mode === 'replay') return view.source || null;
  const id = String(view.output || '').split('/').pop();
  return /^[0-9a-f]{12}$/.test(id) ? id : null;
}

// Progress shows wherever the download was asked for: the replay row, the lobby's shelf,
// the closing card. Only the one on screen is seen.
function exportUi(state) {
  for (const box of document.querySelectorAll('.export')) {
    box.hidden = !state;
    box.querySelector('.export-label').textContent = state ? state.label : '';
    box.querySelector('.export-fill').style.width = state ? `${Math.round(Math.min(1, state.fraction) * 100)}%` : '0';
  }
  $('shelf-actions').hidden = Boolean(state);
  $('finale-actions').hidden = Boolean(state);
  $('cc-actions').hidden = Boolean(state);
}

function saveUrl(url) {
  const link = document.createElement('a');
  link.href = url;
  link.download = '';
  document.body.append(link);
  link.click();
  link.remove();
}

// The music a downloaded video carries. Chosen in the download menu: its last row opens the
// list of loops, and picking one plays a few seconds of it. Nothing here is remembered.
let downloadSound = 'auto', soundTracks = [], soundPreview = null;
// The menus written into the page get the same music row the generated ones carry.
$('finale-download').querySelector('.menu-list').innerHTML = DOWNLOAD_OPTIONS;
for (const list of document.querySelectorAll('#tp-download .menu-list, #shelf-download .menu-list')) {
  if (!list.querySelector('.dl-sound')) list.insertAdjacentHTML('beforeend', SOUND_ROW);
}
if (window.MarioSound) MarioSound.ready.then(list => { soundTracks = list; showDownloadSound(); });

const soundChoices = () => [{id: 'auto', title: 'auto · follows the world'}, ...soundTracks.map(({id, title}) => ({id, title})), {id: 'off', title: 'off · no music'}];
function showDownloadSound() {
  const title = soundTracks.find(item => item.id === downloadSound)?.title;
  const label = downloadSound === 'off' ? 'music · off' : `music · ♪ ${downloadSound === 'auto' ? 'auto' : title || downloadSound}`;
  for (const node of document.querySelectorAll('.dl-sound-label')) node.textContent = label;
  for (const node of document.querySelectorAll('.dl-track')) node.setAttribute('aria-selected', String(node.dataset.value === `sound:${downloadSound}`));
}
// The list of loops, folded into the menu under the music row. A menu always opens with it shut.
function showSoundList(row, open) {
  for (const node of document.querySelectorAll('.dl-tracks')) node.remove();
  for (const node of document.querySelectorAll('.dl-sound')) node.setAttribute('aria-expanded', 'false');
  if (!row || !open) return;
  row.setAttribute('aria-expanded', 'true');
  row.insertAdjacentHTML('afterend', `<div class="dl-tracks">${soundChoices().map(item => `<button type="button" role="option" class="dl-track" data-value="sound:${esc(item.id)}" aria-selected="${item.id === downloadSound}">${esc(item.title)}</button>`).join('')}</div>`);
  placeMenu(row.closest('.menu'));
  row.nextElementSibling.querySelector('[aria-selected=true]')?.scrollIntoView({block: 'nearest'});
}

function previewSound(id) {
  if (soundPreview) { soundPreview.audio.pause(); clearTimeout(soundPreview.timer); soundPreview = null; }
  const track = soundTracks.find(item => item.id === (id === 'auto' ? 'overworld' : id));
  if (!track || (window.MarioSound && MarioSound.choice() !== 'off')) return;   // never over the page's own music
  const audio = new Audio(`/assets/audio/${track.file}`);
  audio.volume = 0.16;                                     // as quiet as the page's own music
  audio.currentTime = track.loopStart;
  audio.play().catch(() => {});
  soundPreview = {audio, timer: setTimeout(() => { audio.pause(); soundPreview = null; }, 6000)};
}

document.addEventListener('click', event => {
  if (!event.target.closest('.menu.actions')) { if (soundPreview) previewSound(null); return; }
  if (event.target.closest('.menu-btn')) { showSoundList(null, false); showDownloadSound(); if (soundPreview) previewSound(null); return; }
  const row = event.target.closest('[role=option][data-value=sound]'), track = event.target.closest('[role=option][data-value^="sound:"]');
  if (!row && !track) { if (soundPreview) previewSound(null); return; }
  event.stopPropagation();                                 // picking the music keeps the menu open
  if (row) { showSoundList(row, row.getAttribute('aria-expanded') !== 'true'); return; }
  downloadSound = track.dataset.value.slice('sound:'.length);
  showDownloadSound();
  previewSound(downloadSound);
}, true);

// Which loop sounds over each stretch of the video, in video seconds. "auto" follows the
// world and crosses into the fork loop while four machines race, as the page does.
function soundPlan(schedule, choice, at, total, fps) {
  if (!choice || choice === 'off') return [];
  const worlds = {1: 'overworld', 2: 'underground', 3: 'sky', 4: 'castle'};
  const idAt = t => {
    if (choice !== 'auto') return choice;
    const show = exportShow(schedule, t);
    if (show.mode === 'grid' && !show.promote) return 'fork';
    let stage = '1-1';
    for (const event of schedule.events) { if (event.elapsed > t) break; if (event.stage) stage = event.stage; }
    return worlds[Number(String(stage).split('-')[1]) || 1] || 'overworld';
  };
  const segments = [];
  for (let index = 0; index < total; index++) {
    const id = idAt(at(index)), last = segments.at(-1);
    if (last?.id === id) last.to = (index + 1) / fps;
    else segments.push({id, from: index / fps, to: (index + 1) / fps});
  }
  return segments;
}

function download(run, kind) {
  if (!run) return;
  if (kind === 'zip') saveUrl(`/api/download/${run}.zip`);
  else renderVideo(run, {cut: kind === 'cut'});
}

// A run that has just ended may still be exporting its timelines; give it a moment.
async function scheduleOf(run) {
  for (let attempt = 0; attempt < 30; attempt++) {
    const response = await fetch(`/api/schedule?run=${run}`);
    if (response.ok) return response.json();
    const problem = (await response.json().catch(() => ({}))).error || 'that recording is not available';
    if (!/still going/.test(problem)) throw new Error(problem);
    if (exporting?.cancelled) throw new Error('cancelled');
    await sleep(500);
  }
  throw new Error('the run is still finishing. try again in a moment.');
}

async function renderVideo(run, options = {}) {
  if (exporting) return null;
  const job = {cancelled: false, run, id: null};
  exporting = job;
  exportUi({fraction: 0, label: 'preparing the video'});
  if (view) renderPlayback(Boolean(view.busy));
  const json = {'Content-Type': 'application/json'};
  try {
    const schedule = await scheduleOf(run);
    EXPORT = EXPORT_LAYOUTS[schedule.game] || EXPORT_LAYOUTS.mario;
    const started = await fetch('/api/render/start', {method: 'POST', headers: json, body: JSON.stringify({fps: 30})});
    if (started.status === 501) {
      // No encoder on the host: the page can still record the replay that is on screen.
      if (view?.mode === 'replay' && view.source === run) { exporting = null; exportUi(null); return exportVideo(); }
      throw new Error('install ffmpeg on the host to download a video (brew install ffmpeg), or replay the recording and download it from there.');
    }
    if (!started.ok) throw new Error((await started.json().catch(() => ({}))).error || 'the render could not start');
    job.id = (await started.json()).job;
    await Promise.all([document.fonts.load('40px GeistPixelCircle'), document.fonts.load('24px GeistPixelSquare')]);

    const fps = 30, canvas = document.createElement('canvas');
    canvas.width = EXPORT.w;
    canvas.height = EXPORT.h;
    const ctx = canvas.getContext('2d');
    const deaths = new Set(schedule.events.filter(event => event.type === 'death').map(event => event.sandbox));
    // "no deaths" renders the surviving verse only: the same frames, walked on the cut's clock
    const canon = options.cut ? buildCanon(schedule) : null;
    const length = canon ? canon.total : schedule.end;
    const at = index => canon ? canon.toFull(Math.min(length, index / fps)) : Math.min(schedule.end, index / fps);
    const wipeAt = index => { const c = index / fps, fold = canon?.folds.find(item => c >= item.c && c < item.c + 0.2); return fold ? (c - fold.c) / 0.2 : null; };
    const total = Math.ceil(length * fps) + fps;              // hold the last picture for a second
    const wanted = t => exportShow(schedule, t).names.map(name => [name, exportFrame(schedule, name, t)]).filter(([, frame]) => frame !== null);
    const post = async (blob, repeat) => {
      const response = await fetch(`/api/render/${job.id}/frames`, {method: 'POST', headers: {'Content-Type': 'application/octet-stream', 'X-Frames': String(repeat)}, body: blob});
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || 'the encoder refused a frame');
    };
    let key = null, blob = null, repeat = 0;
    const flush = async () => { if (blob && repeat) { await post(blob, repeat); repeat = 0; } };
    for (let index = 0; index < total && !job.cancelled; index++) {
      const t = at(index);
      const frames = wanted(t);
      const wipeNow = wipeAt(index);
      const state = JSON.stringify([frames, exportCaption(schedule, t), frames.map(([name]) => deaths.has(name) && t >= (schedule.anchors[name]?.at(-1)?.[0] ?? Infinity)), wipeNow, exportFxActive(schedule, t) ? index : -1]);
      if (state === key) repeat += 1;                         // a held picture is sent once
      else {
        await flush();
        await Promise.all(frames.map(([name, frame]) => fetchFrame(name, frame, run)));
        drawExport(ctx, schedule, t, deaths, {preload: false, canon, wipe: wipeNow ?? undefined});
        blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
        key = state;
        repeat = 1;
      }
      if (repeat >= 600) await flush();
      if (index % 15 === 0) {
        exportUi({fraction: index / total, label: `rendering ${clock(index / fps)} / ${clock(length)}`});
        for (let ahead = 1; ahead <= 45; ahead++) for (const [name, frame] of wanted(at(index + ahead))) fetchFrame(name, frame, run);
      }
    }
    if (job.cancelled) return null;
    await flush();
    exportUi({fraction: 1, label: 'finishing the file'});
    exportUi({fraction: 1, label: downloadSound === 'off' ? 'finishing the file' : 'adding the sound'});
    const finished = await fetch(`/api/render/${job.id}/finish`, {method: 'POST', headers: json, body: JSON.stringify({sound: soundPlan(schedule, downloadSound, at, total, fps)})});
    if (!finished.ok) throw new Error((await finished.json().catch(() => ({}))).error || 'the encoder failed');
    const result = await finished.json();
    if (options.save !== false) saveUrl(`${result.url}?name=${exportGame(schedule).hero}-never-dies-${run}${canon ? '-no-deaths' : ''}.mp4`);
    job.id = null;                                            // keep the file on the host until it is fetched
    return result;
  } catch (error) {
    if (error.message !== 'cancelled') { $('error').textContent = error.message; $('error').hidden = false; }
    return null;
  } finally {
    if (job.id) fetch(`/api/render/${job.id}/cancel`, {method: 'POST', headers: json, body: '{}'}).catch(() => {});
    if (exporting === job) { exporting = null; exportUi(null); }
    if (view) renderPlayback(Boolean(view.busy || ['starting', 'running', 'paused'].includes(view.status)));
  }
}

// ---------------------------------------------------------------- screen recording
//
// ● records the page exactly as it is on screen, in either mode: the browser's own tab
// capture, encoded as it plays. ■ (or ending the share from the browser's bar) stops
// and downloads the file. The picker opens on this tab; one click confirms it.
let recording = null;

function videoMime() {
  return ['video/mp4;codecs=avc1.42E01E', 'video/mp4', 'video/webm;codecs=vp9', 'video/webm'].find(type => MediaRecorder.isTypeSupported(type));
}

function saveBlob(blob, name) {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 60_000);
}

async function startRecording() {
  if (recording) return;
  const mime = window.MediaRecorder && videoMime();
  if (!navigator.mediaDevices?.getDisplayMedia || !mime) {
    $('error').textContent = 'this browser cannot record the screen';
    $('error').hidden = false;
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: {frameRate: {ideal: 60}},
      audio: false,
      preferCurrentTab: true,
      selfBrowserSurface: 'include',
      surfaceSwitching: 'exclude',
      monitorTypeSurfaces: 'exclude',
    });
  } catch {
    return;   // the picker was dismissed
  }
  const recorder = new MediaRecorder(stream, {mimeType: mime, videoBitsPerSecond: 16_000_000});
  const chunks = [];
  recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
  recording = {recorder, stream, chunks, mime, started: performance.now(), timer: setInterval(renderRecording, 250)};
  stream.getVideoTracks()[0].addEventListener('ended', () => stopRecording());
  recorder.start(1000);
  document.body.classList.add('recording');
  renderRecording();
}

async function stopRecording() {
  if (!recording) return;
  const {recorder, stream, chunks, mime, timer, started} = recording;
  recording = null;
  clearInterval(timer);
  document.body.classList.remove('recording');
  if (recorder.state !== 'inactive') await new Promise(resolve => { recorder.onstop = resolve; recorder.stop(); });
  for (const track of stream.getTracks()) track.stop();
  renderRecording();
  renderPlayback(Boolean(view?.busy || ['starting', 'running', 'paused'].includes(view?.status)));
  if (!chunks.length || performance.now() - started < 300) return;
  const when = new Date();
  const stamp = `${when.getFullYear()}${String(when.getMonth() + 1).padStart(2, '0')}${String(when.getDate()).padStart(2, '0')}-${String(when.getHours()).padStart(2, '0')}${String(when.getMinutes()).padStart(2, '0')}${String(when.getSeconds()).padStart(2, '0')}`;
  saveBlob(new Blob(chunks, {type: mime}), `${G().hero}-never-dies-${stamp}.${mime.startsWith('video/mp4') ? 'mp4' : 'webm'}`);
}

function renderRecording() {
  const on = Boolean(recording);
  const button = $('tp-record');
  button.classList.toggle('on', on);
  $('tp-record-stop').disabled = !on;
  const label = on ? 'recording · r stops it' : 'record the screen · r';
  button.setAttribute('aria-label', label);
  button.title = label;
  $('rec-time').hidden = !on;
  if (on) $('rec-time').textContent = clock((performance.now() - recording.started) / 1000);
}
$('tp-record').onclick = () => startRecording();
$('tp-record-stop').onclick = () => stopRecording();

// ---------------------------------------------------------------- fullscreen
//
// The page itself fills the screen: the game, the timeline and the dock, without the browser
// around them. f does the same; esc is the browser's own way out.
const fullscreenOn = () => Boolean(document.fullscreenElement || document.webkitFullscreenElement);
function toggleFullscreen() {
  const root = document.documentElement;
  const call = fullscreenOn() ? (document.exitFullscreen || document.webkitExitFullscreen)?.call(document) : (root.requestFullscreen || root.webkitRequestFullscreen)?.call(root);
  call?.catch?.(() => {});                                  // refused (an embedded page): nothing to do
}
function renderFullscreen() {
  const on = fullscreenOn(), button = $('tp-fullscreen'), label = on ? 'leave fullscreen · f' : 'fullscreen · f';
  button.setAttribute('aria-pressed', String(on));
  button.setAttribute('aria-label', label);
  button.title = label;
  $('ic-full').toggleAttribute('hidden', on);
  $('ic-unfull').toggleAttribute('hidden', !on);
}
$('tp-fullscreen').hidden = !(document.fullscreenEnabled || document.webkitFullscreenEnabled);
$('tp-fullscreen').onclick = () => toggleFullscreen();
for (const name of ['fullscreenchange', 'webkitfullscreenchange']) document.addEventListener(name, renderFullscreen);

// ---------------------------------------------------------------- render loop

let uiVersion = null;
function render(next) {
  const previous = view;
  const joining = !run;
  view = next;
  // The server was restarted with a newer page: reload rather than run stale code.
  if (view.ui) { if (uiVersion === null) uiVersion = view.ui; else if (uiVersion !== view.ui) { location.reload(); return; } }
  const running = view.busy || ['starting', 'running', 'paused'].includes(view.status);
  if (!run || (view.output && run.output !== view.output)) {
    run = freshRun(view.output);
    run.serial = view.serial || 0;
    if (view.mode === 'replay') loadSchedule(run);
    $('finale').hidden = true;
    $('screen').replaceChildren();
    $('ops').replaceChildren();
    $('focus').textContent = '';
    forkFocus = null;
    dockTab = 'moment';
    renderPeek();
    if (view.events.length && (joining || !running)) {
      // Joining an existing run must show its current fork immediately, rather
      // than replay old death/rewind animations against the latest VM states.
      run.lastEventT = run.playedT = view.events.at(-1).t;
      for (const event of view.events) { tally(event); run.ops.push(...opsFor(event)); }
      renderOps();
      if (running) setFocus(view.paused ? 'paused here. press space to play.' : 'one machine. another chance.');
    }
  }
  if ((view.serial || 0) !== run.serial) { run.serial = view.serial; catchUp(); }
  if (running && !leaving) watched = true;
  // A replay cut short by stop goes back to the start screen; one that reached its end,
  // and any live run, keeps its closing card.
  const cutShort = view.mode === 'replay' && Boolean(view.duration) && view.elapsed < view.duration - 0.05;
  if (ENDED.has(view.status) && !running) {
    // No more views arrive once a run has ended, so look again after the grace: a run that
    // ended without a closing card (stopped at its last frame) still gets its lobby back.
    if (!run.endedAt) { run.endedAt = Date.now(); setTimeout(() => { if (view === next) render(next); }, 1600); }
  } else run.endedAt = null;
  const closing = !$('finale').hidden || (run.endedAt && Date.now() - run.endedAt < 1500);
  // The run on screen decides the game. A lobby shows when nothing is running or closing.
  // An ended run this page never watched is only the server's last state: it must not
  // pull the lobby of another game over to its own.
  if (view.game && GAMES[view.game] && (running || (watched && ENDED.has(view.status) && closing)) && game !== view.game) {
    game = view.game;
    recordings = allRuns.filter(item => gameOf(item) === game);
    featured = null;
  }
  if (running) chosen = true;
  // A run that was left is gone from the page at once, even while its machines are still being cleaned up.
  if (leaving && !view.busy) leaving = false;
  syncLobby(leaving || !(running || (watched && ENDED.has(view.status) && !cutShort && Boolean(closing))));
  $('stop').hidden = !running;
  $('live').disabled = !view.live_enabled || !view.key_configured;
  $('live').title = !view.live_enabled ? 'start with uv run mnd to enable live microvm play' : !view.key_configured ? 'TYPESAFE_API_KEY is not configured on the host' : '';
  if (!$('idle').hidden && !$('hint').textContent) $('hint').textContent = !view.live_enabled ? 'live play needs the launcher: uv run mnd. replays need no key and no vms.' : !view.key_configured ? 'set TYPESAFE_API_KEY to play live.' : '';

  // The world, centred in the bar. Live or replay is told by the playback row, not here.
  const pipes = Math.max(0, ...view.timelines.filter(item => ['trunk', 'candidate', 'cleared'].includes(item.role)).map(item => Number(item.score) || 0));
  const label = view.mode === 'simulation' ? '<span>simulation</span>' : game === 'bird' ? `<span>pipes</span><b>${pipes}</b>` : `<span>world</span><b>${esc(view.stage || '1-1')}</b>`;
  // The bar speaks for the run on screen; in the lobby or the game menu there is none.
  const shown = (running || ENDED.has(view.status)) && !lobbyWanted ? label : '';
  if ($('world').innerHTML !== shown) $('world').innerHTML = shown;

  for (const event of view.events) {
    if (event.t <= run.lastEventT) continue;
    run.lastEventT = event.t;
    enqueue(event);
  }
  for (const key of ['snapshots', 'deaths', 'verses']) $(`c-${key}`).textContent = lobbyWanted ? 0 : run.counts[key];
  $('c-snapshots').classList.toggle('ours', !lobbyWanted && run.counts.snapshots > 0);
  // alive: the machines running right now. four during a split, one otherwise.
  const alive = view.timelines.filter(item => ['trunk', 'candidate'].includes(item.role) && item.phase !== 'dead').length;
  $('c-alive').textContent = lobbyWanted ? 0 : view.status === 'complete' ? 1 : alive;
  if (run.drag?.released && Math.abs(view.elapsed - run.drag.elapsed) < 0.75) { run.drag = null; run.dragT = null; if (run.peek && !run.peek.pinned) { run.peek = null; renderPeek(); } }
  updateCursor();

  canonHop();
  renderPlayback(running);
  renderMap();
  syncScreen();
  const failure = view.error;
  if (failure && previous?.error !== failure) { $('error').textContent = failure; $('error').hidden = false; }
}

// State arrives by server-sent events the moment it is published; polling is the fallback.
let pollTimer = null;
async function poll() {
  pollTimer = null;
  try {
    const response = await fetch('/api/state');
    if (!response.ok) throw new Error('server unavailable');
    render(await response.json());
  } catch (error) {
    if (!(error instanceof TypeError)) console.error(error);
  } finally {
    if (pollTimer !== false) pollTimer = window.setTimeout(poll, 100);
  }
}

function connect() {
  if (!('EventSource' in window)) { poll(); return; }
  const source = new EventSource('/api/stream');
  source.onmessage = event => {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = false; }
    try { render(JSON.parse(event.data)); } catch (error) { console.error(error); }
  };
  source.onerror = () => { if (pollTimer === null || pollTimer === false) { pollTimer = null; poll(); } };
}

// ---------------------------------------------------------------- resizing
//
// Two edges can be dragged: the top of the timeline section (its height) and the line before
// the dock (the dock's width). Both are the CSS variables the layout already uses, set on
// <body> so they win over a layout's own defaults; kept per layout in this browser. A double
// click puts an edge back. The phone layout keeps its own sizes.
const LAYOUT_KEY = 'mnd.layout';
const EDGES = {map: {name: '--map', handle: 'resize-map', row: true}, dock: {name: '--dock', handle: 'resize-dock', row: false}};
const layoutMode = () => (document.body.classList.contains('tall') ? 'tall' : 'wide');
function savedLayout() { try { return JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {}; } catch { return {}; } }
function saveLayout(key, value) {
  const all = savedLayout(), mode = layoutMode();
  all[mode] = {...all[mode]};
  if (value === null) delete all[mode][key]; else all[mode][key] = value;
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(all)); } catch { /* a private window: the size lasts until reload */ }
}
// The game keeps at least 160 px of height, the timeline at least 320 px of width.
function clampEdge(key, value) {
  const main = document.querySelector('main');
  if (key === 'map') return Math.round(Math.max(140, Math.min(value, main.clientHeight - $('deck').offsetHeight - (layoutMode() === 'tall' ? 120 : 160 + document.querySelector('.focus').offsetHeight))));
  return Math.round(Math.max(240, Math.min(value, $('map-section').clientWidth - 320, 760)));
}
function relayout() { if (!view) return; renderMap(); if (dockTab === 'multiverse') renderMultiverse(); }
function applyLayout() {
  const saved = savedLayout()[layoutMode()] || {}, phone = innerWidth <= 760;
  for (const [key, edge] of Object.entries(EDGES)) {
    if (phone || typeof saved[key] !== 'number') document.body.style.removeProperty(edge.name);
    else document.body.style.setProperty(edge.name, `${clampEdge(key, saved[key])}px`);
  }
}
for (const [key, edge] of Object.entries(EDGES)) {
  const handle = $(edge.handle);
  const size = () => (edge.row ? $('map-section').offsetHeight : $('dock').offsetWidth);
  const set = value => { const next = clampEdge(key, value); document.body.style.setProperty(edge.name, `${next}px`); return next; };
  handle.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    handle.classList.add('dragging');
    document.body.classList.add('resizing', edge.row ? 'by-row' : 'by-col');
    const from = size(), start = edge.row ? event.clientY : event.clientX;
    let value = from, queued = false;
    // both edges grow towards the top left: up makes the timeline taller, left makes the dock wider
    const move = moved => {
      value = set(from + start - (edge.row ? moved.clientY : moved.clientX));
      if (queued) return;
      queued = true;
      (document.hidden ? setTimeout : requestAnimationFrame)(() => { queued = false; relayout(); });
    };
    const done = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', done);
      handle.removeEventListener('pointercancel', done);
      handle.classList.remove('dragging');
      document.body.classList.remove('resizing', 'by-row', 'by-col');
      if (value !== from) saveLayout(key, value);
      relayout();
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', done);
    handle.addEventListener('pointercancel', done);
  });
  handle.addEventListener('dblclick', () => { saveLayout(key, null); document.body.style.removeProperty(edge.name); relayout(); });
  handle.addEventListener('keydown', event => {
    const step = {ArrowUp: 1, ArrowLeft: 1, ArrowDown: -1, ArrowRight: -1}[event.key];
    if (!step || (edge.row ? !/Up|Down/.test(event.key) : !/Left|Right/.test(event.key))) return;
    event.preventDefault();
    event.stopPropagation();
    saveLayout(key, set(size() + step * (event.shiftKey ? 48 : 16)));
    relayout();
  });
}
// the other layout (a tall game beside the timeline) brings its own saved sizes and limits
let wasTall = document.body.classList.contains('tall');
new MutationObserver(() => {
  const tall = document.body.classList.contains('tall');
  if (tall === wasTall) return;
  wasTall = tall;
  applyLayout();
  relayout();
}).observe(document.body, {attributes: true, attributeFilter: ['class']});
applyLayout();

window.addEventListener('resize', () => { applyLayout(); if (view) renderMap(); });
loadRuns();
connect();
