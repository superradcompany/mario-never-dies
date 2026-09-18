// Music that plays along with the run: one seamless loop at a time, chosen by the world,
// by a fork and by the finish, or picked by hand.
//
// It never starts by itself. Every page load begins silent, the choice is not remembered,
// and nothing sounds until someone picks a track (or "auto") from the menu or presses m.
//
// Every file in assets/audio carries a second of its own tail in front of the loop and a
// second of its own head behind it. Looping between the stated points therefore cannot
// click: whatever delay a codec adds shifts both points onto the same waveform.
(function () {
  const FADE = 0.9;            // seconds to cross from one loop to the next
  const LEVEL = 0.16;          // background level: the loops sit well under everything else
  const WORLDS = {1: 'overworld', 2: 'underground', 3: 'sky', 4: 'castle'};
  const SCAN_RATE = {1: 1, 2: 1.5, 4: 2.2, 8: 3};
  const SPEED_RATE = {0.5: 0.75, 1: 1, 2: 1.35, 4: 1.8, 8: 2.4};

  let context = null, master = null, tone = null;
  let tracks = [], byId = new Map();
  const buffers = new Map(), loading = new Map(), backwards = new Map();
  let playing = null;                                   // {key, source, gain}
  let wanted = {id: null, reverse: false, rate: 1, dimmed: false};
  let choice = 'off', lastOn = 'auto';

  const ready = fetch('/assets/audio/loops.json').then(response => response.json()).then(list => {
    tracks = list;
    byId = new Map(list.map(item => [item.id, item]));
    return list;
  }).catch(() => []);

  function ensure() {
    if (context) return context;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return null;
    context = new AudioContextClass({latencyHint: 'playback'});
    master = context.createGain();
    master.gain.value = LEVEL;
    tone = context.createBiquadFilter();
    tone.type = 'lowpass';
    tone.frequency.value = 20000;
    tone.Q.value = 0.8;
    tone.connect(master);
    master.connect(context.destination);
    return context;
  }

  function load(id) {
    if (buffers.has(id)) return Promise.resolve(buffers.get(id));
    if (loading.has(id)) return loading.get(id);
    const job = fetch(`/assets/audio/${byId.get(id).file}`)
      .then(response => response.arrayBuffer())
      .then(bytes => context.decodeAudioData(bytes))
      .then(buffer => { buffers.set(id, buffer); loading.delete(id); return buffer; })
      .catch(() => { loading.delete(id); return null; });
    loading.set(id, job);
    return job;
  }

  // A rewind plays the loop backwards: just the loop's own body, reversed once and kept.
  function reversed(id) {
    if (backwards.has(id)) return backwards.get(id);
    const item = byId.get(id), buffer = buffers.get(id);
    const from = Math.floor(item.loopStart * buffer.sampleRate), to = Math.floor(item.loopEnd * buffer.sampleRate);
    const copy = context.createBuffer(buffer.numberOfChannels, to - from, buffer.sampleRate);
    for (let channel = 0; channel < buffer.numberOfChannels; channel++) {
      const samples = buffer.getChannelData(channel).slice(from, to);
      samples.reverse();
      copy.copyToChannel(samples, channel);
    }
    backwards.set(id, copy);
    return copy;
  }

  function begin(id, reverse) {
    const item = byId.get(id);
    const source = context.createBufferSource();
    source.loop = true;
    if (reverse) source.buffer = reversed(id);
    else { source.buffer = buffers.get(id); source.loopStart = item.loopStart; source.loopEnd = item.loopEnd; }
    source.playbackRate.value = wanted.rate;
    const gain = context.createGain();
    gain.gain.setValueAtTime(0, context.currentTime);
    gain.gain.linearRampToValueAtTime(1, context.currentTime + (reverse ? 0.15 : FADE));
    source.connect(gain);
    gain.connect(tone);
    source.start(0, reverse ? 0 : item.loopStart);
    return {key: `${id}:${reverse}`, source, gain};
  }

  function end(node, seconds) {
    const now = context.currentTime;
    node.gain.gain.cancelScheduledValues(now);
    node.gain.gain.setValueAtTime(node.gain.gain.value, now);
    node.gain.gain.linearRampToValueAtTime(0, now + seconds);
    node.source.stop(now + seconds + 0.05);
  }

  async function apply() {
    if (!context || context.state !== 'running') return;
    const now = context.currentTime;
    tone.frequency.setTargetAtTime(wanted.dimmed ? 520 : 20000, now, 0.14);
    master.gain.setTargetAtTime(wanted.dimmed ? LEVEL * 0.55 : LEVEL, now, 0.14);
    const {id, reverse} = wanted;
    if (!id || !byId.has(id)) { if (playing) { end(playing, FADE); playing = null; } return; }
    const key = `${id}:${reverse}`;
    if (playing?.key === key) { playing.source.playbackRate.setTargetAtTime(wanted.rate, now, 0.08); return; }
    if (!buffers.has(id)) {
      await load(id);
      if (wanted.id !== id || wanted.reverse !== reverse || !buffers.has(id)) return apply();
    }
    if (playing?.key === key) return;
    const next = begin(id, reverse);
    if (playing) end(playing, reverse || playing.key.endsWith(':true') ? 0.15 : FADE);
    playing = next;
  }

  // Browsers only let sound start from a gesture. Any click or key is one.
  function unlock() {
    if (!ensure()) return;
    if (context.state === 'suspended' && !document.hidden) context.resume().then(apply);
  }
  for (const type of ['pointerdown', 'keydown']) window.addEventListener(type, unlock, true);
  document.addEventListener('visibilitychange', () => {
    if (!context) return;
    if (document.hidden) context.suspend();
    else context.resume().then(apply);
  });

  function trackFor(stage) {
    const level = Number(String(stage || '1-1').split('-')[1]) || 1;
    return WORLDS[level] || 'overworld';
  }

  // What should be sounding for a given moment of the run. Pure, so it can be checked.
  function decide(state) {
    let id = null;
    if (choice === 'off') id = null;
    else if (state.won) id = choice === 'auto' ? 'victory' : choice;
    else if (!state.running) id = null;
    else if (choice === 'auto') id = state.fork ? 'fork' : trackFor(state.stage);
    else id = choice;
    const scanning = Boolean(state.scan);
    return {
      id,
      reverse: scanning && state.scan < 0,
      rate: scanning ? (SCAN_RATE[Math.abs(state.scan)] || 1) : (SPEED_RATE[state.speed] || 1),
      dimmed: Boolean(state.paused) && !scanning && !state.won,
    };
  }

  // Called on every view update with where the run is; cheap when nothing changed.
  function sync(state) {
    const next = decide(state);
    const id = next.id;
    const changed = next.id !== wanted.id || next.reverse !== wanted.reverse || next.rate !== wanted.rate || next.dimmed !== wanted.dimmed;
    wanted = next;
    if (changed) apply();
    if (context && id && choice === 'auto') for (const ahead of [trackFor(state.stage), 'fork']) if (byId.has(ahead) && !buffers.has(ahead)) load(ahead);
  }

  function choose(value) {
    choice = value;
    if (value !== 'off') lastOn = value;
  }

  window.MarioSound = {
    ready,
    sync,
    decide,
    choose,
    choice: () => choice,
    toggle: () => { choose(choice === 'off' ? lastOn : 'off'); return choice; },
    now: () => playing?.key.split(':')[0] || null,
  };
})();
