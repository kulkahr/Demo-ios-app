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
import os
import shutil
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

# --- Karaoke timing estimator (structural model) -----------------------------
# Vagdhenu synthesizes the verse as ONE CONTINUOUS clip: the shard passes the
# padas joined into a single synthesis piece (space-joined) — the same shape as
# the official demo's Renderer.render_one(), which splits text only on
# dandas/newlines. (Passing each pada as its own clip instead makes render.py
# stitch per-word clips with 0.55s silences and gate-trim short words like
# ॐ/स्वः/नः down to near-nothing — chopped audio.) Word onsets therefore
# follow a steady chant pace: each word's span is proportional to its
# akshara count across the real WAV duration. (With no inter-word gaps, the
# meter's sec_per_syllable and rescaling cancel out of the math.)
# PARITY: Mantra/Services/TimingEstimator.swift, scripts/kaggle_render.py and
# backend/modal_app.py implement the same rule (architecture.md §5.3).
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


def estimate_timing(padas: list[str], duration: float, meter: str | None = None) -> list[dict]:
    """Structural karaoke timings for a continuously-rendered verse: each
    word's span is proportional to its akshara count across the real WAV
    duration. Every entry starts exactly where the previous one ends, so a
    word stays highlighted until the next one sounds — short words are never
    skipped. `meter` is accepted for API compatibility with the per-clip-gap
    model this replaces; pacing is uniform within a single continuous clip,
    so the meter cancels out of the math."""
    n = len(padas)
    if n == 0 or duration <= 0:
        return []
    weights = [akshara_count(p) for p in padas]
    total = sum(weights)
    timing: list[dict] = []
    cum = 0
    for i, pada in enumerate(padas):
        start = duration * cum / total
        cum += weights[i]
        end = duration if i == n - 1 else min(duration * cum / total, duration)
        timing.append(
            {"text": pada, "start": round(start, 3), "end": round(end, 3), "estimated": True}
        )
    return timing


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return frames / float(rate) if rate else 0.0


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
                "padas": [" ".join(m["padas"])],
                "seed": 42,
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
            timing = estimate_timing(m["padas"], duration, meter=m["meter"])
            (TIMING_OUT_DIR / f"{m['stableID']}.json").write_text(
                json.dumps(timing, ensure_ascii=False, indent=2)
            )
            print(f"✓ {m['stableID']}: {dest.name} ({duration:.2f}s, {len(m['padas'])} padas)")

    print("\nCorpus rendered. mantras/ is bundled automatically by project.yml (folder reference).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
