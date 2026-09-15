#!/usr/bin/env python3
"""Render the mantra corpus with Vagdhenu.

Reads mantras/mantras.json, builds a Vagdhenu shard JSON, invokes Vagdhenu's
render pipeline on a CUDA 12.1 machine, then copies the rendered WAVs into
mantras/audio/ and writes karaoke timing sidecars into mantras/timing/.

Prerequisites (do NOT run in this sandbox — requires GPU):
    git clone https://github.com/prathoshap/vagdhenu
    cd vagdhenu && bash scripts/setup.sh   # torch+cu121, deps, BigVGAN, weights

Usage:
    VAGDHENU_ROOT=/path/to/vagdhenu python3 scripts/render_corpus.py

Outputs:
    mantras/audio/<stableID>.wav
    mantras/timing/<stableID>.json   # [{"text","start","end","estimated"}, ...]
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_PATH = REPO_ROOT / "mantras" / "mantras.json"
AUDIO_OUT_DIR = REPO_ROOT / "mantras" / "audio"
TIMING_OUT_DIR = REPO_ROOT / "mantras" / "timing"
VAGDHENU_ROOT = Path(os.environ.get("VAGDHENU_ROOT", "~/vagdhenu")).expanduser()

# --- Karaoke timing estimator (phrase-aware structural model) ----------------
# Vagdhenu synthesizes the verse as ONE CONTINUOUS clip: the shard passes the
# padas joined into a single synthesis piece (space-joined) — the same shape as
# the official demo's Renderer.render_one(), which splits text only on
# dandas/newlines. (Passing each pada as its own clip instead makes render.py
# stitch per-word clips with 0.55s silences and gate-trim short words like
# ॐ/स्वः/नः down to near-nothing — chopped audio.)
#
# The model breathes between phrase groups, so a purely uniform distribution
# drifts late in the second half of longer verses (observed up to ~0.85s on
# the Śānti Pāṭha). The phrase-aware estimator below measures the REAL
# silences from the rendered WAV (frame energy) and absorbs each pause into
# the word immediately before it — mid-verse pauses therefore delay nothing
# after them, and boundaries stay locked to the real audio.
# PARITY: Mantra/Services/TimingEstimator.swift (uniform fallback for custom
# verses), scripts/kaggle_render.py and backend/modal_app.py implement the
# same rule (architecture.md §5.3).
VIRAMAS = ("्", "್")  # Devanagari + Kannada virāma


def akshara_count(pada: str) -> int:
    """Syllables in a pada — port of render.py's n_aksharas on Devanagari
    (plus its Kannada ranges, since prep_text accepts any Brahmic script).
    Independent vowels and non-halant consonants count 1; a consonant followed
    by virāma starts a cluster and adds nothing; ॐ chants as one syllable
    (oṃ, via the Kannada-routed model text); matras, anusvāra, visarga,
    joiners and dandas add nothing."""
    n = 0
    for i, ch in enumerate(pada):
        o = ord(ch)
        if 0x0905 <= o <= 0x0914 or 0x0C85 <= o <= 0x0C94:
            n += 1  # independent vowels
        elif 0x0915 <= o <= 0x0939 or 0x0C95 <= o <= 0x0CB9:
            nxt = pada[i + 1] if i + 1 < len(pada) else ""
            if nxt not in VIRAMAS:
                n += 1  # consonant onset (halant consonants join the next)
        elif o == 0x0950:  # ॐ
            n += 1
    return max(n, 1)


def wav_energy(path: Path, win_ms: float = 20.0) -> tuple[float, list[float]]:
    """Frame-RMS envelope of a PCM wav: (duration, rms list in 0..1)."""
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        duration = wf.getnframes() / float(rate) if rate else 0.0
        width = wf.getsampwidth()
        data = wf.readframes(wf.getnframes())
    width = max(width, 1)
    n = len(data) // width
    fmt = {1: "B", 2: "h", 4: "i"}.get(width, "h")
    samples = struct.unpack(f"<{n}{fmt}", data[: n * width])
    if width == 1:  # unsigned 8-bit
        samples = [s - 128 for s in samples]
    win = max(int(rate * win_ms / 1000.0), 1)
    rms = []
    for i in range(0, len(samples), win):
        chunk = samples[i : i + win]
        r = math.sqrt(sum(s * s for s in chunk) / max(len(chunk), 1)) / 32768.0
        rms.append(r)
    return duration, rms


def measure_pauses(
    path: Path,
    padas: list[str],
    total_aksharas: int,
    win_ms: float = 20.0,
    silence_rms: float = 0.02,
    min_silence_s: float = 0.30,
    min_gap_s: float = 0.35,
    margin_s: float = 0.03,
    max_pauses: int = 16,
) -> list[float]:
    """Real silences in the rendered WAV, attributed to the word before each.

    Frame-energy scan: consecutive frames below `silence_rms` spanning at
    least `min_silence_s` are a pause (ignoring anything in the first/last
    `min_gap_s` of the file — leading/trailing room tone is not a phrase
    pause). Each pause is attributed to the word whose proportional boundary
    it follows, minus a small `margin_s` so the highlight cuts off just
    before the breath rather than inside it.
    """
    if not path.exists() or total_aksharas <= 0 or not padas:
        return []
    duration, rms = wav_energy(path, win_ms=win_ms)
    if duration <= 0 or not rms:
        return []
    n = len(padas)
    # Proportional boundaries at frame resolution — a pause right after
    # boundary j is attributed to word j.
    boundaries: list[float] = []
    cum = 0
    for p in padas[:-1]:
        cum += akshara_count(p)
        boundaries.append(duration * cum / total_aksharas)

    spans: list[tuple[float, float]] = []
    in_silence = False
    silence_start = 0.0
    for i, r in enumerate(rms):
        t = i * win_ms / 1000.0
        if r < silence_rms and not in_silence:
            in_silence = True
            silence_start = t
        elif r >= silence_rms and in_silence:
            in_silence = False
            if t - silence_start >= min_silence_s and min_gap_s < silence_start:
                spans.append((silence_start, t))
    if in_silence and duration - silence_start >= min_silence_s and min_gap_s < silence_start:
        spans.append((silence_start, duration))

    pauses = [0.0] * n
    attributed = 0
    for pause_start, pause_end in spans:
        if attributed >= max_pauses:
            break
        # Attribute to the last boundary at or before the pause start.
        owner = 0
        for j, b in enumerate(boundaries):
            if b <= pause_start + win_ms / 1000.0:
                owner = j + 1
        pauses[owner] = max(0.0, pause_end - pause_start - margin_s)
        attributed += 1
    return pauses


def estimate_timing(
    padas: list[str],
    duration: float,
    meter: str | None = None,
    wav_path: Path | None = None,
) -> list[dict]:
    """Phrase-aware structural karaoke timings.

    Base model: the verse is one continuous clip, so each word's speech span
    is proportional to its akshara count. When `wav_path` is given, the REAL
    silences are measured from the rendered audio: speech spans are scaled to
    the non-silent duration (duration − pauses) and each measured pause is
    appended after its word — so onsets after a mid-verse breath move
    EARLIER (matching the real chant) instead of drifting late. Every entry
    still starts exactly where the previous one ends — short words are never
    skipped, and the final entry always ends at `duration`. `meter` is
    accepted for API compatibility; pacing is uniform within the clip, so
    the meter cancels out of the math.
    """
    n = len(padas)
    if n == 0 or duration <= 0:
        return []
    weights = [akshara_count(p) for p in padas]
    total = sum(weights)

    pauses = measure_pauses(wav_path, padas, total) if wav_path else []
    if len(pauses) != n:
        pauses = [0.0] * n  # no/unmeasurable audio: pure uniform plan
    pause_total = sum(pauses)
    if pause_total >= duration:
        pauses = [0.0] * n  # pathological measurement: fall back to uniform
        pause_total = 0.0
    scale = (duration - pause_total) / total  # speech seconds per akshara

    timing: list[dict] = []
    cursor = 0.0
    for i, pada in enumerate(padas):
        start = cursor
        cursor += weights[i] * scale
        end = duration if i == n - 1 else min(cursor + pauses[i], duration)
        cursor = end
        timing.append(
            {"text": pada, "start": round(start, 3), "end": round(end, 3), "estimated": True}
        )
    return timing


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return frames / float(rate) if rate else 0.0


def model_text_from_padas(padas: list[str]) -> str:
    """Space-joined synthesis text for one continuous clip.

    ॐ is kept as-is: it and the long-ō spelling ओं both route to the same
    SLP1 token "oM" (→ Kannada ಒಂ), so respelling cannot change the render.
    The leading OM being swallowed is a sampling issue, not text: F5-TTS is
    seeded-stochastic and some takes drop the short-o hum — seed 60 (the
    demo's slider default) renders it reliably, seed 42 did not.
    """
    return " ".join(padas)


def build_shard(mantras: list[dict], workdir: Path) -> Path:
    # no_sandhi is REQUIRED by Vagdhenu's render.py (KeyError per clip if
    # missing). true = text is already traditionally word-split; skip the
    # renderer's automatic sandhi re-splitting.
    #
    # ONE CONTINUOUS PIECE per mantra: padas are space-joined into a single
    # synthesis clip — exactly what the official demo's Renderer.render_one()
    # does for danda-free text. (A shard entry of per-word padas makes
    # render.py synthesize one clip per word and stitch them with 0.55s
    # silences; short clips get gate-trimmed to near-nothing — chopped audio.)
    # The `meter` is still required: it selects the reference chant (unknown
    # names fall back to vasantatilakā by design).
    shard = []
    for m in mantras:
        shard.append(
            {
                "id": m["stableID"],
                "meter": m["meter"],
                "padas": [model_text_from_padas(m["padas"])],
                # seed 60 = the demo's slider default (and the seed behind the
                # demo's published audio). F5 is seeded-stochastic; seed 42's
                # takes swallowed the leading OM (Kannada ಒಂ) in the Gāyatrī.
                "seed": 60,
                "no_sandhi": True,
                "out": m["audioFileName"],
            }
        )
    shard_path = workdir / "shard.json"
    shard_path.write_text(json.dumps(shard, ensure_ascii=False, indent=2))
    return shard_path


def main() -> int:
    if not CORPUS_PATH.exists():
        print(f"error: corpus not found at {CORPUS_PATH}", file=sys.stderr)
        return 1
    if not VAGDHENU_ROOT.exists():
        print(
            f"error: VAGDHENU_ROOT not found at {VAGDHENU_ROOT}\n"
            "Clone https://github.com/prathoshap/vagdhenu and run scripts/setup.sh first.",
            file=sys.stderr,
        )
        return 1

    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    mantras = corpus["mantras"]

    AUDIO_OUT_DIR.mkdir(parents=True, exist_ok=True)
    TIMING_OUT_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="vagdhenu-corpus-") as tmp:
        workdir = Path(tmp)
        shard_path = build_shard(mantras, workdir)

        render_script = VAGDHENU_ROOT / "src" / "render.py"
        if not render_script.exists():
            print(f"error: {render_script} not found", file=sys.stderr)
            return 1

        cmd = [
            sys.executable,
            str(render_script),
            "--shard",
            str(shard_path),
            "--results",
            str(workdir / "results.json"),
            "--outdir",
            str(workdir),
            # nfe 32 = the demo server's VAGDHENU_NFE default (render.py's own
            # default is 64). Matches the demo's audio and halves render time.
            "--nfe",
            "32",
        ]
        print("running:", " ".join(cmd))
        subprocess.run(cmd, cwd=str(VAGDHENU_ROOT), check=True)

        for m in mantras:
            produced = workdir / m["audioFileName"]
            if not produced.exists():
                # Vagdhenu may prefix output with the shard id.
                alt = workdir / f"{m['stableID']}_{m['audioFileName']}"
                produced = alt if alt.exists() else produced
            if not produced.exists():
                print(f"error: render for {m['stableID']} produced no wav", file=sys.stderr)
                return 1

            dest = AUDIO_OUT_DIR / m["audioFileName"]
            shutil.copyfile(produced, dest)
            duration = wav_duration(dest)
            timing = estimate_timing(m["padas"], duration, meter=m["meter"], wav_path=dest)
            (TIMING_OUT_DIR / f"{m['stableID']}.json").write_text(
                json.dumps(timing, ensure_ascii=False, indent=2)
            )
            print(f"✓ {m['stableID']}: {dest.name} ({duration:.2f}s, {len(m['padas'])} padas)")

    print("\nCorpus rendered. mantras/ is bundled automatically by project.yml (folder reference).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
