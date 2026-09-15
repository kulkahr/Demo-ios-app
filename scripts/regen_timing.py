#!/usr/bin/env python3
"""Regenerate karaoke timing sidecars from already-rendered WAVs — no GPU.

Reads each mantra's WAV (mantras/audio/<audioFileName>), recomputes the
structural timing plan (same estimator as scripts/render_corpus.py), and
rewrites mantras/timing/<stableID>.json in place. Run this after upgrading
from the old proportional timing sidecars: karaoke sync then matches the
existing audio without re-rendering anything.

Usage (repo root):
    python3 scripts/regen_timing.py

Only sidecars whose text matches the corpus padas are rewritten (a mismatch
would mean the corpus JSON changed since the sidecar was written).
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from render_corpus import (  # noqa: E402  (single source of truth for the estimator)
    CORPUS_PATH,
    TIMING_OUT_DIR,
    estimate_timing,
    wav_duration,
)


def main() -> int:
    if not CORPUS_PATH.exists():
        print(f"error: corpus not found at {CORPUS_PATH}", file=sys.stderr)
        return 1
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    TIMING_OUT_DIR.mkdir(parents=True, exist_ok=True)

    failures = 0
    for m in corpus["mantras"]:
        wav_path = REPO_ROOT / "mantras" / "audio" / m["audioFileName"]
        if not wav_path.exists():
            print(f"– {m['stableID']}: no WAV at mantras/audio/{m['audioFileName']}, skipped")
            failures += 1
            continue

        duration = wav_duration(wav_path)
        timing = estimate_timing(m["padas"], duration, meter=m["meter"])
        out_path = TIMING_OUT_DIR / f"{m['stableID']}.json"

        # Safety: only overwrite a sidecar whose words still match the corpus.
        if out_path.exists():
            try:
                old = json.loads(out_path.read_text(encoding="utf-8"))
                old_texts = [e["text"] for e in old]
                if old_texts != m["padas"]:
                    print(
                        f"! {m['stableID']}: sidecar words no longer match the corpus; "
                        "remove it to force a rewrite",
                        file=sys.stderr,
                    )
                    failures += 1
                    continue
            except json.JSONDecodeError:
                pass  # corrupt sidecar: rewrite it

        out_path.write_text(json.dumps(timing, ensure_ascii=False, indent=2))
        print(f"✓ {m['stableID']}: {out_path.name} ({duration:.2f}s, {len(m['padas'])} padas)")

    if failures:
        print(f"\n{failures} mantra(s) skipped — see notes above.", file=sys.stderr)
        return 1
    print("\nSidecars regenerated. Rebuild in Xcode so the bundle picks them up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
