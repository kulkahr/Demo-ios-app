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

# Long vowels (guru, 2 moras): independent letters + dependent matra signs.
# Weights are per code point — NOT per grapheme cluster — because dependent
# matra signs combine into clusters with their consonant and would otherwise
# be invisible. Mirrors backend/modal_app.py and TimingEstimator.swift.
LONG_MATRAS = set(
    "आईऊॠॡएऐओऔ"  # independent long vowels
    "ाीूॄेैोौ"  # dependent matra signs: ā ī ū ṝ e ai o au
)
SHORT_MATRAS = set(
    "अइउऋऌ"  # independent short vowels
    "िुृॢ"  # dependent signs: i u ṛ ḷ
)
ANUSVARA_VISARGA = set("ँंः")  # candrabindu, anusvāra, visarga → guru
SKIP_SCALARS = set("्\u200c\u200d।॥")  # virāma, ZWNJ/ZWJ, dandas


def syllable_weight(pada: str) -> int:
    """Estimate moraic weight of a pada: guru units count 2, laghu 1 (min 1)."""
    weight = 0
    for ch in pada:
        if ch in LONG_MATRAS or ch in ANUSVARA_VISARGA:
            weight += 2
        elif ch in SHORT_MATRAS:
            weight += 1
        elif ch == "्" or ch in SKIP_SCALARS:  # virāma, joiners, dandas
            continue
        else:
            # Consonant/vowel akshara without a matra: conservative laghu.
            weight += 1
    return max(weight, 1)


def estimate_timing(padas: list[str], duration: float) -> list[dict]:
    """Distribute duration across padas weighted by syllable weight."""
    weights = [syllable_weight(p) for p in padas]
    total = sum(weights)
    timing: list[dict] = []
    cursor = 0.0
    for pada, w in zip(padas, weights):
        span = duration * (w / total)
        end = round(min(cursor + span, duration), 3)
        timing.append(
            {"text": pada, "start": round(cursor, 3), "end": end, "estimated": True}
        )
        cursor = end
    # Snap the final boundary exactly to the duration.
    if timing:
        timing[-1]["end"] = round(duration, 3)
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
            timing = estimate_timing(m["padas"], duration)
            (TIMING_OUT_DIR / f"{m['stableID']}.json").write_text(
                json.dumps(timing, ensure_ascii=False, indent=2)
            )
            print(f"✓ {m['stableID']}: {dest.name} ({duration:.2f}s, {len(m['padas'])} padas)")

    print("\nCorpus rendered. mantras/ is bundled automatically by project.yml (folder reference).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
