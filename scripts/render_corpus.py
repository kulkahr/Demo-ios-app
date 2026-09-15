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
# Vagdhenu synthesizes each pada as its own clip and stitches them with fixed
# gaps (render.py: --gap 0.55s, +0.20s when the clip ends in virāma; the
# trailing gap is dropped). Per-clip speech length is aksharas × the meter's
# sec_per_syll from src/reference_bank/bank.json. Word onsets therefore follow
# that structure; the speech rate is then globally rescaled so the plan spans
# the real WAV — the residual (gate()/F5 trim) scales with speech, while the
# gaps are exact digital silence.
# PARITY: Mantra/Services/TimingEstimator.swift, scripts/kaggle_render.py and
# backend/modal_app.py implement the same rule (architecture.md §5.3).

METER_SPS = {
    # ASCII stems (render.py's WAV-name keys) and IAST bank keys, s/syllable.
    "anushtubh": 0.326, "anuṣṭubh": 0.326,
    "pramanika": 0.275, "pramāṇikā": 0.275,
    "vasantatilaka": 0.259, "vasantatilakā": 0.259,
    "upajati": 0.273, "upajāti": 0.273,
    "indravajra": 0.260, "indravajrā": 0.260,
    "upendravajra": 0.269, "upendravajrā": 0.269,
    "vamshastha": 0.255, "vaṃśastha": 0.255,
    "rathoddhata": 0.301, "rathoddhatā": 0.301,
    "shalini": 0.320, "śālinī": 0.320,
    "indravamsha": 0.268, "indravaṃśā": 0.268,
    "drutavilambita": 0.369,
    "bhujangaprayata": 0.303, "bhujaṅgaprayāta": 0.303,
    "malini": 0.268, "mālinī": 0.268,
    "shardulavikridita": 0.273, "śārdūlavikrīḍita": 0.273,
    "sragdhara": 0.310, "sragdharā": 0.310,
    "vrutta1": 0.437, "vrutta-1": 0.437,
    "gadya": 0.260, "gadya_mbtn": 0.260,
}
DEFAULT_SPS = 0.259   # render.py's unknown-meter fallback is vasantatilakā
GAP_S = 0.55          # render.py --gap
GAP_HALANT_S = 0.20   # render.py --gap_halant (added after a virāma-final clip)
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


def meter_sps(meter: str | None) -> float:
    return METER_SPS.get((meter or "").strip().lower(), DEFAULT_SPS)


def estimate_timing(padas: list[str], duration: float, meter: str | None = None) -> list[dict]:
    """Structural karaoke timings: word onsets follow Vagdhenu's synthesis
    structure (per-pada speech + fixed inter-pada gaps). Each entry spans from
    its speech onset to the next entry's onset, so a word stays highlighted
    through the pause after it and switches exactly at the next onset — short
    words are never skipped while still sounding."""
    n = len(padas)
    if n == 0 or duration <= 0:
        return []
    sps = meter_sps(meter)
    speech = [akshara_count(p) * sps for p in padas]
    gaps = [GAP_S + (GAP_HALANT_S if p.endswith(VIRAMAS) else 0.0) for p in padas]
    gap_total = sum(gaps[:-1])  # the stitcher drops the trailing gap
    speech_total = sum(speech)
    if speech_total > 0 and duration > gap_total:
        k = (duration - gap_total) / speech_total
        speech = [s * k for s in speech]
    onsets: list[float] = []
    t = 0.0
    for i, s in enumerate(speech):
        onsets.append(t)
        t += s + (gaps[i] if i < n - 1 else 0.0)
    timing: list[dict] = []
    for i, pada in enumerate(padas):
        start = min(onsets[i], duration)
        end = min(onsets[i + 1] if i + 1 < n else duration, duration)
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
    # missing). true = padas are already traditionally word-split.
    shard = []
    for m in mantras:
        shard.append(
            {
                "id": m["stableID"],
                "meter": m["meter"],
                "padas": m["padas"],
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
