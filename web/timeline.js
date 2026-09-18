'use strict';

// Build ancestry from checkpoint identities, never positions: two copies can be at
// the same x while belonging to completely different attempts.
const MarioTimeline = (() => {
  function build(events, items, trunk) {
    const states = new Map(items.map(item => [item.name, item]));
    const segments = new Map(), nodes = new Map(), forks = [];
    for (const event of events) {
      if (event.type === 'created') segments.set(event.sandbox, {name: event.sandbox, x0: 40, checkpoints: [], event});
      if (event.type === 'checkpoint') {
        nodes.set(event.child, {slot: event.child, x: event.x_pos, on: event.sandbox, frame: event.frame, elapsed: event.elapsed});
        segments.get(event.sandbox)?.checkpoints.push(event.child);
      }
      if (event.type === 'rewind' || event.type === 'multiverse') {
        const node = nodes.get(event.parent);
        if (event.type === 'multiverse') forks.push(event);
        const children = event.type === 'rewind' ? [event.sandbox] : event.children;
        children.forEach((name, index) => segments.set(name, {
          name, x0: node?.x ?? 40, parent: node?.on, slot: event.parent,
          checkpoints: [], event, index, experiment: event.experiments?.[name],
        }));
      }
    }
    for (const segment of segments.values()) {
      const item = states.get(segment.name);
      segment.role = item?.role || 'retired';
      segment.x1 = item?.x_pos ?? segment.x0;
      // A host time limit can retire a living Mario; reserve death marks for deaths.
      segment.dead = segment.role === 'dead' || item?.phase === 'dead';
    }
    const path = [], onPath = new Set();
    let cursor = trunk, limit = Infinity, departure = null;
    while (segments.has(cursor) && !onPath.has(cursor)) {
      const segment = segments.get(cursor);
      path.unshift({...segment, x1: Math.min(limit, segment.x1), departure,
        checkpoints: segment.checkpoints.filter(slot => nodes.get(slot).x <= limit)});
      onPath.add(cursor);
      limit = segment.x0;
      departure = segment.slot;
      cursor = segment.parent;
    }
    const branches = [];
    for (const segment of segments.values()) {
      const part = path.find(part => part.name === segment.name);
      if (part) {
        // An ancestor's abandoned tail is still a failed future, even though the
        // beginning of that very same machine remains in the surviving history.
        if (part.departure && (segment.x1 > part.x1 || segment.dead)) {
          branches.push({...segment, x0: nodes.get(part.departure)?.x ?? part.x1,
            slot: part.departure, parent: segment.name, tail: true});
        }
      } else branches.push({...segment, tail: false});
    }
    return {segments, nodes, forks, path, onPath, branches};
  }

  function curve(x0, y0, x1, y1) {
    // A short future must remain selectable, including an attempt that died
    // without advancing. The endpoint's actual position is shown in inspection.
    const end = Math.max(x0 + 28, x1);
    const bend = Math.min(90, (end - x0) * .55);
    return {end, d: `M${x0} ${y0} C${x0 + bend} ${y0} ${x0 + bend} ${y1} ${end} ${y1}`};
  }

  function visibleBranches(branches, winner, limit = 4) {
    const racing = branches.filter(branch => branch.role === 'candidate' || branch.name === winner);
    const recent = branches.filter(branch => !racing.includes(branch)).slice(-Math.max(0, limit - racing.length));
    return [...recent, ...racing].slice(-limit);
  }

  return {build, curve, visibleBranches};
})();

// The browser and Node tests exercise the same ancestry and layout code.
if (typeof module !== 'undefined') module.exports = MarioTimeline;
