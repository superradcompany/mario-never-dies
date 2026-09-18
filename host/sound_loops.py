"""Make the music loops the page plays along with the game.

    uv run --with numpy --with scipy python host/sound_loops.py generate
    uv run --with numpy --with scipy python host/sound_loops.py loop
    uv run --with numpy --with scipy python host/sound_loops.py verify

`generate` asks ElevenLabs Music for one instrumental piece per entry in
host/sound_tracks.json (ELEVENLABS_API_KEY in the environment; an existing source is never
regenerated, so nothing is paid for twice). `loop` cuts the most loopable bar-aligned window
out of each piece and writes web/assets/audio/<id>.mp3 plus loops.json. `verify` decodes
what was delivered and measures every loop point.

A loop file carries a second of its own tail in front of the loop and a second of its own
head behind it. Looping between the stated points is then immune to codec delay: whatever
shift a decoder adds lands both points on the same waveform.
"""

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACKS = ROOT / "host" / "sound_tracks.json"
SOURCES = ROOT / "runs" / "sound-sources"
AUDIO = ROOT / "web" / "assets" / "audio"
API = "https://api.elevenlabs.io"
RATE = 44100
HOP = 512
PAD = 1.0  # seconds of wrap on each side of the loop
SHORT_FADE = 0.045


def generate():
    key = os.environ["ELEVENLABS_API_KEY"]
    SOURCES.mkdir(parents=True, exist_ok=True)
    for track in json.loads(TRACKS.read_text()):
        target = SOURCES / f"{track['id']}.mp3"
        if target.exists():
            print(f"{track['id']}: source exists, skipped")
            continue
        body = {
            "prompt": track["prompt"],
            "music_length_ms": track["ms"],
            "model_id": "music_v2",
            "force_instrumental": True,
        }
        request = urllib.request.Request(
            f"{API}/v1/music",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"xi-api-key": key, "Content-Type": "application/json"},
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                target.write_bytes(response.read())
        except urllib.error.HTTPError as error:
            print(
                f"{track['id']}: failed {error.code} {error.read().decode(errors='replace')[:400]}"
            )
            continue
        print(
            f"{track['id']}: {target.stat().st_size / 1e6:.2f} MB in {time.time() - started:.0f}s"
        )


def decode(path):
    import numpy as np
    from scipy.io import wavfile

    wav = path.with_suffix(".decoded.wav")
    command = ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-ar", str(RATE), "-ac", "2"]
    subprocess.run([*command, "-c:a", "pcm_s16le", str(wav)], check=True)
    _, data = wavfile.read(wav)
    wav.unlink()
    return data.astype(np.float32) / 32768.0


def describe(mono):
    """Log-spaced band energies per frame, and how much they rise from frame to frame."""
    import numpy as np
    from scipy.signal import stft

    _, _, spectrum = stft(
        mono, fs=RATE, nperseg=2048, noverlap=2048 - HOP, padded=False, boundary=None
    )
    magnitude = np.abs(spectrum)
    freqs = np.linspace(0, RATE / 2, magnitude.shape[0])
    edges = np.geomspace(60, 12000, 49)
    bands = np.stack(
        [
            magnitude[(freqs >= lo) & (freqs < hi)].sum(axis=0)
            for lo, hi in zip(edges[:-1], edges[1:], strict=True)
        ]
    )
    bands = np.log1p(30 * bands)
    rise = np.concatenate([[0], np.maximum(0, np.diff(bands, axis=1)).sum(axis=0)])
    return bands, rise


def tempo(rise, hint):
    import numpy as np

    envelope = rise - rise.mean()
    auto = np.correlate(envelope, envelope, mode="full")[len(envelope) - 1 :]
    frames = RATE / HOP
    lags = np.arange(len(auto))

    def strength(bpm):
        lag = 60.0 / bpm * frames
        return sum(np.interp(lag * beats, lags, auto) for beats in (1, 2, 4, 8))

    return float(max(np.arange(hint * 0.92, hint * 1.08, 0.05), key=strength))


def junction(body):
    """How much the sound changes where the loop wraps, against its ordinary strong changes."""
    import numpy as np
    from scipy.signal import stft

    twice = np.concatenate([body, body]).mean(axis=1)
    _, _, spectrum = stft(twice, fs=RATE, nperseg=1024, noverlap=768, padded=False, boundary=None)
    change = np.abs(np.diff(np.log1p(30 * np.abs(spectrum)), axis=1)).sum(axis=0)
    at = len(body) // 256
    return float(change[at - 3 : at + 3].max() / np.percentile(change, 90))


def cut(track):
    import numpy as np
    from scipy.io import wavfile

    audio = decode(SOURCES / f"{track['id']}.mp3")
    mono = audio.mean(axis=1)
    bands, rise = describe(mono)
    frames = RATE / HOP
    bpm = tempo(rise, track["bpm"])
    bar = 4 * 60.0 / bpm
    seconds = len(mono) / RATE
    bars = next(
        (n for n in (track["bars"], 16, 12, 8, 4) if n <= track["bars"] and n * bar <= seconds - 5),
        4,
    )
    unit = bands / (np.linalg.norm(bands, axis=0, keepdims=True) + 1e-9)
    window, lead = int(1.6 * frames), int(0.5 * frames)
    best = None
    # What follows the end must sound like what follows the start, and likewise just before.
    for length in np.arange(bars * bar * 0.985, bars * bar * 1.015, HOP / RATE):
        span = int(round(length * frames))
        first = int(1.5 * frames)
        for start in range(first, max(first + 1, unit.shape[1] - span - window - 1)):
            after = (
                unit[:, start : start + window] * unit[:, start + span : start + span + window]
            ).sum(axis=0)
            before = (
                unit[:, start - lead : start] * unit[:, start + span - lead : start + span]
            ).sum(axis=0)
            score = 0.7 * float(after.mean()) + 0.3 * float(before.mean())
            if best is None or score > best[0]:
                best = (score, start, span)
    score, start, span = best
    near = int(0.12 * frames)
    start = max(0, start - near) + int(np.argmax(rise[max(0, start - near) : start + near]))
    begin = max(0, start * HOP - int(0.004 * RATE))
    finish = begin + span * HOP
    # line the end up with the start at sample level
    reference = mono[begin : begin + 4096]
    shifts = range(-600, 601)
    finish += max(
        shifts, key=lambda k: float(np.dot(reference, mono[finish + k : finish + k + 4096]))
    )
    # crossfade: short for dry chip sounds, up to two beats where tails cross the seam
    beat = 60.0 / bpm
    choices = []
    for fade_seconds in (SHORT_FADE, 0.12, beat / 2, beat, 2 * beat):
        fade = min(int(fade_seconds * RATE), len(audio) - finish - 1)
        body = audio[begin:finish].copy()
        ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
        body[:fade] = (
            audio[finish : finish + fade] * (1 - ramp) + audio[begin : begin + fade] * ramp
        )
        choices.append((junction(body), body))
    jump, body = min(choices, key=lambda choice: choice[0])
    # the same loudness for every loop, with headroom
    level = float(np.sqrt((body**2).mean()))
    body *= min(0.11 / (level + 1e-9), 0.89 / (np.abs(body).max() + 1e-9))
    pad = int(PAD * RATE)
    wrapped = np.concatenate([body[-pad:], body, body[:pad]])
    AUDIO.mkdir(parents=True, exist_ok=True)
    wav = AUDIO / f"{track['id']}.wav"
    wavfile.write(wav, RATE, (np.clip(wrapped, -1, 1) * 32767).astype(np.int16))
    mp3 = AUDIO / f"{track['id']}.mp3"
    encode = ["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "160k"]
    subprocess.run([*encode, str(mp3)], check=True)
    wav.unlink()
    fit = f"{len(body) / RATE:6.2f}s {bars:>2} bars @ {bpm:6.2f}"
    print(f"{track['id']:<12} {fit} match {score:.3f} jump {jump:.2f}")
    return {
        "id": track["id"],
        "title": track["title"],
        "file": mp3.name,
        "loopStart": PAD,
        "loopEnd": round(PAD + len(body) / RATE, 5),
        "bpm": round(bpm, 2),
        "bars": bars,
        "seconds": round(len(body) / RATE, 2),
        "role": track["role"],
    }


def loop():
    tracks = [t for t in json.loads(TRACKS.read_text()) if (SOURCES / f"{t['id']}.mp3").exists()]
    manifest = [cut(track) for track in tracks]
    (AUDIO / "loops.json").write_text(json.dumps(manifest, indent=1) + "\n")


def verify():
    import numpy as np

    for item in json.loads((AUDIO / "loops.json").read_text()):
        audio = decode(AUDIO / item["file"])
        worst = 0.0
        for shift in (0, 0.013, 0.025, 0.05):  # any codec delay must leave the seam intact
            a, b = int((item["loopStart"] + shift) * RATE), int((item["loopEnd"] + shift) * RATE)
            twice = np.concatenate([audio[a:b], audio[a:b]]).mean(axis=1)
            steps = np.abs(np.diff(twice))
            seam = steps[b - a - 2 : b - a + 2].max() / (np.percentile(steps, 99.9) + 1e-9)
            worst = max(worst, float(seam))
        body = audio[int(item["loopStart"] * RATE) : int(item["loopEnd"] * RATE)]
        level = 20 * np.log10(np.sqrt((body**2).mean()) + 1e-9)
        room = len(audio) / RATE - item["loopEnd"]
        detail = f"level {level:.1f} dB tail {room:.2f}s"
        print(f"{item['id']:<12} click {worst:.2f} (under 1 is inaudible) {detail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=("generate", "loop", "verify"))
    {"generate": generate, "loop": loop, "verify": verify}[parser.parse_args().step]()
