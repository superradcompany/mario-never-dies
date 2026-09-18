const {test} = require('node:test');
const assert = require('node:assert/strict');
const {build, curve, visibleBranches} = require('../web/timeline.js');
const event = (type, fields) => ({type, ...fields});

test('a rewind preserves ancestry and shows the dead original tail', () => {
  const graph = build([
    event('created', {sandbox: 'original'}),
    event('checkpoint', {sandbox: 'original', child: 'copy', x_pos: 100}),
    event('rewind', {sandbox: 'new', parent: 'copy'}),
  ], [{name: 'original', role: 'dead', x_pos: 200}, {name: 'new', role: 'trunk', x_pos: 150}], 'new');
  assert.deepEqual(graph.path.map(s => [s.name, s.x0, s.x1]), [['original', 40, 100], ['new', 100, 150]]);
  assert.deepEqual(graph.branches.map(s => [s.name, s.slot, s.x0, s.x1, s.tail]), [['original', 'copy', 100, 200, true]]);
});

test('copies at equal positions remain attached to their own parent VM', () => {
  const graph = build([
    event('created', {sandbox: 'first'}),
    event('checkpoint', {sandbox: 'first', child: 'a', x_pos: 100}),
    event('rewind', {sandbox: 'second', parent: 'a'}),
    event('checkpoint', {sandbox: 'second', child: 'b', x_pos: 100}),
    event('multiverse', {parent: 'b', children: ['win', 'lose'], experiments: {win: {steps: [{action: 'noop', frames: 16}]}}}),
  ], [{name: 'first', role: 'dead', x_pos: 200}, {name: 'second', role: 'dead', x_pos: 160}, {name: 'win', role: 'trunk', x_pos: 300}, {name: 'lose', role: 'dead', x_pos: 110}], 'win');
  assert.equal(graph.segments.get('lose').parent, 'second');
  assert.equal(graph.segments.get('lose').slot, 'b');
  assert.deepEqual(graph.path.map(s => s.name), ['first', 'second', 'win']);
  assert.equal(graph.segments.get('win').experiment.steps[0].frames, 16);
  assert.equal(graph.branches.some(s => s.name === 'win'), false);
});

test('seeking backwards removes futures that have not happened yet', () => {
  const created = event('created', {sandbox: 'first'});
  const copy = event('checkpoint', {sandbox: 'first', child: 'a', x_pos: 100});
  const graph = build([created, copy], [{name: 'first', role: 'trunk', x_pos: 120}], 'first');
  assert.equal(graph.branches.length, 0);
  assert.equal(graph.forks.length, 0);
  assert.equal(graph.path[0].x1, 120);
});

test('all ancestry survives more than 100 controller events', () => {
  const events = [event('created', {sandbox: 'first'})];
  for (let i = 0; i < 120; i++) events.push(event('checkpoint', {sandbox: 'first', child: `copy${i}`, x_pos: 100 + i}));
  events.push(event('rewind', {sandbox: 'second', parent: 'copy119'}));
  const graph = build(events, [{name: 'first', role: 'dead', x_pos: 250}, {name: 'second', role: 'trunk', x_pos: 230}], 'second');
  assert.equal(graph.nodes.size, 120);
  assert.deepEqual(graph.path.map(s => s.name), ['first', 'second']);
});

test('active race gets space while older attempts stay available in history', () => {
  const branches = Array.from({length: 20}, (_, i) => ({name: String(i), role: i < 16 ? 'dead' : 'candidate'}));
  assert.deepEqual(visibleBranches(branches, null).map(s => s.name), ['16', '17', '18', '19']);
  assert.equal(branches.length, 20);
  assert.deepEqual(visibleBranches(branches.slice(0, 16), null).map(s => s.name), ['12', '13', '14', '15']);
});

test('a zero-progress or backward attempt still has a selectable curve', () => {
  for (const end of [100, 90, 150]) {
    const shape = curve(100, 35, end, 85);
    assert.ok(shape.end >= 128);
    assert.match(shape.d, /^M100 35 C/);
    assert.ok(!shape.d.includes('NaN'));
  }
});

test('time and approach limits do not label a living branch as dead', () => {
  for (const role of ['timed_out', 'approach_exhausted', 'dead']) {
    const graph = build([
      event('created', {sandbox: 'original'}),
      event('checkpoint', {sandbox: 'original', child: 'copy', x_pos: 100}),
      event('multiverse', {parent: 'copy', children: ['trial']}),
    ], [{name: 'trial', role, phase: role === 'dead' ? 'dead' : 'deciding', x_pos: 150}], 'original');
    assert.equal(graph.segments.get('trial').dead, role === 'dead');
  }
});
