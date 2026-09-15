"""Render the mantra corpus on Modal's GPU (no local CUDA needed).

Vagdhenu requires CUDA 12.1, which rules out Macs and other machines
without an NVIDIA GPU: the pinned cu121 torch wheels are not built for
macOS. This one-off job runs the same shard pipeline as
`scripts/render_corpus.py` inside the GPU container defined in
`backend/modal_app.py`, then downloads the produced WAVs and timing
sidecars into `mantras/audio/` and `mantras/timing/`.

Usage (from the repo root):

    python3 -m pip install modal
    python3 -m modal setup            # one-time browser login
    python3 -m modal run backend/render_corpus_modal.py

The first run builds the container image (downloads torch + model
weights) and takes several minutes; subsequent runs are much faster.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import modal

from modal_app import app

VAGDHENU_DIR = "/root/vagdhenu"
REPO_ROOT = Path(__file__).resolve().parent.parent


@app.function(
    gpu="A10G",
    timeout=3600,
    mounts=[
        # Reuse the estimator/shard logic from the local corpus script so
        # timing parity stays single-sourced in scripts/render_corpus.py.
        modal.Mount.from_local_file(
            local_path=REPO_ROOT / "scripts" / "render_corpus.py",
            remote_path="/root/render_corpus.py",
        )
    ],
)
def render_corpus(corpus: dict) -> dict[str, bytes]:
    """Render every mantra in `corpus`; return WAVs + timing sidecars."""
    sys.path.insert(0, "/root")
    from render_corpus import build_shard, estimate_timing, wav_duration

    mantras = corpus["mantras"]
    outputs: dict[str, bytes] = {}

    with tempfile.TemporaryDirectory(prefix="corpus-") as tmp:
        workdir = Path(tmp)
        shard_path = build_shard(mantras, workdir)

        cmd = [
            sys.executable,
            f"{VAGDHENU_DIR}/src/render.py",
            "--shard", str(shard_path),
            "--results", str(workdir / "results.json"),
            "--outdir", str(workdir),
        ]
        print("running:", " ".join(cmd))
        subprocess.run(cmd, cwd=VAGDHENU_DIR, check=True)

        for m in mantras:
            produced = workdir / m["audioFileName"]
            if not produced.exists():
                # Vagdhenu may prefix output with the shard id.
                alt = workdir / f"{m['stableID']}_{m['audioFileName']}"
                produced = alt if alt.exists() else produced
            if not produced.exists():
                raise RuntimeError(f"render for {m['stableID']} produced no wav")

            duration = wav_duration(produced)
            outputs[m["audioFileName"]] = produced.read_bytes()
            timing = estimate_timing(m["padas"], duration, meter=m["meter"])
            outputs[f"{m['stableID']}.json"] = json.dumps(
                timing, ensure_ascii=False, indent=2
            ).encode()
            print(f"✓ {m['stableID']}: {m['audioFileName']} ({duration:.2f}s)")

    return outputs


@app.local_entrypoint()
def main() -> None:
    corpus = json.loads(
        (REPO_ROOT / "mantras" / "mantras.json").read_text(encoding="utf-8")
    )
    outputs = render_corpus.remote(corpus)

    audio_dir = REPO_ROOT / "mantras" / "audio"
    timing_dir = REPO_ROOT / "mantras" / "timing"
    audio_dir.mkdir(parents=True, exist_ok=True)
    timing_dir.mkdir(parents=True, exist_ok=True)

    wav_count = 0
    for name, data in outputs.items():
        if name.endswith(".wav"):
            (audio_dir / name).write_bytes(data)
            print(f"wrote mantras/audio/{name} ({len(data):,} bytes)")
            wav_count += 1
        else:
            (timing_dir / name).write_bytes(data)
            print(f"wrote mantras/timing/{name}")

    print(f"\nDone: {wav_count} WAVs + {len(outputs) - wav_count} timing sidecars.")
    print("Rebuild in Xcode — the mantras/ folder reference picks these up automatically.")


if __name__ == "__main__":
    main()
