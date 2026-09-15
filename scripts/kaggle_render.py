#!/usr/bin/env python3
"""Render the mantra corpus on Kaggle's free GPU — no local CUDA needed.

Kaggle gives free T4/P100 GPU sessions (~30 h/week), which is more than
enough for this one-off render. Vagdhenu requires CUDA, so Macs/CI cannot
run it locally; this script runs the whole pipeline on Kaggle instead.

HOW TO USE (takes ~15–25 minutes total):

1. Go to kaggle.com → Code → New Notebook.
2. Notebook options (right panel):
     - Accelerator: GPU T4 x2  (or P100)
     - Internet: ON           (requires a phone-verified Kaggle account)
3. Paste THIS ENTIRE FILE into the first cell and run it.
4. When it finishes, open the notebook's Output tab and download
   `mantra_corpus.zip`.
5. On your Mac, unzip and copy the contents into this repo so that:
       Demo-ios-app/mantras/audio/*.wav
       Demo-ios-app/timing/*.json     <- i.e. mantras/timing/*.json
   are in place, then rebuild in Xcode. The `mantras/` folder reference
   bundles new files automatically (no `xcodegen generate` needed).

If the Vagdhenu render step fails because its CLI changed upstream, paste
the error from the cell output back into the chat — the invocation lives
in `run_render()` below and is the only Vagdhenu-specific line.
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

WORK = Path("/kaggle/working")
VAGDHENU = WORK / "vagdhenu"
CORPUS_OUT = WORK / "mantra_corpus"

# Version pinning: Vagdhenu's setup.sh installs torch==2.4.1 (cu121) but
# leaves Kaggle's preinstalled torchvision/torchaudio untouched. Those were
# compiled against Kaggle's much newer torch (CUDA 13 based), so they die at
# import (`operator torchvision::nms does not exist`, `libcudart.so.13: can-
# not open shared object file` — the latter surfacing through the
# f5_tts -> torchaudio chain). The pins below are the releases paired with
# torch 2.4.1 in the same cu121 index Vagdhenu's setup uses.
CU121_INDEX = "https://download.pytorch.org/whl/cu121"
TORCH_PIN = "2.4.1"
TORCHVISION_PIN = "0.19.1"
TORCHAUDIO_PIN = "2.4.1"

# --- Corpus: embedded from mantras/mantras.json so no GitHub auth is needed.
# Keep in sync with the repo file (schemaVersion 1).
MANTRAS = [
    {
        "stableID": "gayatri",
        "title": "Gāyatrī Mantra",
        "transliteration": "oṃ bhūr bhuvaḥ svaḥ tat savitur vareṇyaṃ bhargo devasya dhīmahi dhiyo yo naḥ pracodayāt",
        "padas": [
            "ॐ", "भूर्भुवः", "स्वः", "तत्सवितुर्वरेण्यम्", "भर्गो",
            "देवस्य", "धीमहि", "धियो", "यो", "नः", "प्रचोदयात्",
        ],
        "meter": "gayatri",
        "meaning": "We meditate on the radiant light of Savitur (the divine sun); may it illuminate our intellect.",
        "sourceName": "Ṛgveda 3.62.10",
        "audioFileName": "gayatri.wav",
    },
    {
        "stableID": "mahamrityunjaya",
        "title": "Mahāmṛtyuñjaya Mantra",
        "transliteration": "tryambakaṃ yajāmahe sugandhiṃ puṣṭivardhanam urvārukam iva bandhanān mṛtyor mukṣīya māmṛtāt",
        "padas": [
            "त्र्यम्बकं", "यजामहे", "सुगन्धिं", "पुष्टिवर्धनम्",
            "उर्वारुकमिव", "बन्धनान्", "मृत्योर्मुक्षीय", "मामृतात्",
        ],
        "meter": "anushtubh",
        "meaning": "We worship the three-eyed Lord Shiva, fragrant and nourishing; may He liberate us from the bondage of death, like a cucumber from its stalk — but not from immortality.",
        "sourceName": "Ṛgveda 7.59.12",
        "audioFileName": "mahamrityunjaya.wav",
    },
    {
        "stableID": "sahanavavatu",
        "title": "Saha Nāv Avatu (Śānti Pāṭha)",
        "transliteration": "oṃ saha nāv avatu saha nau bhunaktu saha vīryaṃ karavāvahai tejasvi nāv adhītam astu mā vidviṣāvahai oṃ śāntiḥ śāntiḥ śāntiḥ",
        "padas": [
            "ॐ", "सह", "नाववतु", "सह", "नौ", "भुनक्तु", "सह", "वीर्यं",
            "करवावहै", "तेजस्वि", "नावधीतमस्तु", "मा", "विद्विषावहै",
            "ॐ", "शान्तिः", "शान्तिः", "शान्तिः",
        ],
        "meter": "anushtubh",
        "meaning": "May the Lord protect and nourish us together; may our study be radiant and free of enmity. Peace, peace, peace.",
        "sourceName": "Taittirīya Upaniṣad 2.2.2 (Kṛṣṇa Yajurveda)",
        "audioFileName": "sahanavavatu.wav",
    },
]

# --- Timing estimator: mirrors scripts/render_corpus.py on your Mac.
# The verse is rendered as ONE CONTINUOUS clip (padas space-joined in the
# shard — same shape as the official demo's Renderer.render_one(); per-word
# clips get stitched with 0.55s silences and short words are gate-trimmed to
# near-nothing). Each word's karaoke span is proportional to its akshara
# count across the WAV duration. Keep all implementations in parity —
# TimingEstimator.swift, backend/modal_app.py.
VIRAMAS = ("्", "್")  # Devanagari + Kannada virāma


def akshara_count(pada: str) -> int:
    """Syllables in a pada — port of render.py's n_aksharas (Devanagari +
    Kannada ranges). Independent vowels and non-halant consonants count 1; a
    consonant followed by virāma starts a cluster and adds nothing; ॐ chants
    as one syllable; matras/anusvāra/visarga/joiners/dandas add nothing."""
    n = 0
    for i, ch in enumerate(pada):
        o = ord(ch)
        if 0x0905 <= o <= 0x0914 or 0x0C85 <= o <= 0x0C94:
            n += 1
        elif 0x0915 <= o <= 0x0939 or 0x0C95 <= o <= 0x0CB9:
            nxt = pada[i + 1] if i + 1 < len(pada) else ""
            if nxt not in VIRAMAS:
                n += 1
        elif o == 0x0950:  # ॐ
            n += 1
    return max(n, 1)


def model_text_from_padas(padas: list[str]) -> str:
    """Space-joined synthesis text for one continuous clip.

    ॐ is kept as-is: it and the long-ō spelling ओं both route to the same
    SLP1 token "oM" (→ Kannada ಒಂ), so respelling cannot change the render.
    The leading OM being swallowed is a sampling issue, not text: F5-TTS is
    seeded-stochastic and some takes drop the short-o hum — seed 60 (the
    demo's slider default) renders it reliably, seed 42 did not.
    """
    return " ".join(padas)


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
    """Phrase-aware structural karaoke timings (mirrors render_corpus.py).

    Base model: the verse is one continuous clip, so each word's speech span
    is proportional to its akshara count. When `wav_path` is given, the REAL
    silences are measured from the rendered audio: speech spans are scaled to
    the non-silent duration (duration − pauses) and each measured pause is
    appended after its word — so onsets after a mid-verse breath move
    EARLIER (matching the real chant) instead of drifting late. Every entry
    still starts exactly where the previous one ends — short words are never
    skipped, and the final entry always ends at `duration`.
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
        timing.append({"text": pada, "start": round(start, 3), "end": round(end, 3), "estimated": True})
    return timing


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        return wf.getnframes() / float(rate) if rate else 0.0


def sh(cmd: list[str] | str, cwd: Path | None = None, env: dict | None = None) -> None:
    print("$", cmd if isinstance(cmd, str) else " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True, shell=isinstance(cmd, str), env=env)


def make_interpreter_shim(shim_dir: Path) -> None:
    """Force `python`/`pip` inside setup.sh to resolve to the kernel env.

    On Kaggle the shell's `pip` can point at a different Python (e.g.
    /opt/conda) than the notebook kernel (/usr/bin/python3). Packages then
    land in an environment render.py never sees — the symptom is
    `ModuleNotFoundError: No module named 'bigvgan'` despite a successful
    setup. Bash wrappers pinned to sys.executable fix that.
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    exe = Path(sys.executable).resolve()
    wrappers = {
        "python": f'exec "{exe}" "$@"',
        "python3": f'exec "{exe}" "$@"',
        "pip": f'exec "{exe}" -m pip "$@"',
        "pip3": f'exec "{exe}" -m pip "$@"',
    }
    for name, body in wrappers.items():
        wrapper = shim_dir / name
        wrapper.write_text(f"#!/bin/bash\n{body}\n")
        wrapper.chmod(0o755)
    os.environ["PATH"] = f"{shim_dir}:{os.environ.get('PATH', '')}"


def render_env() -> dict:
    """Env for Vagdhenu scripts: repo root + src + BigVGAN on PYTHONPATH.

    render.py does bare `import prep_text, bigvgan`: prep_text sits in src/,
    and bigvgan is the NVIDIA/BigVGAN repo cloned by setup.sh into
    <vagdhenu>/BigVGAN — it is NOT a pip package (no setup.py). setup.sh only
    exports its PYTHONPATH inside its own shell, so the kernel must add it
    here for both verification and the render subprocess.
    """
    env = dict(os.environ)
    parts = [
        str(VAGDHENU),
        str(VAGDHENU / "src"),
        str(VAGDHENU / "BigVGAN"),
    ]
    if existing := env.get("PYTHONPATH"):
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def probe_torch_stack() -> tuple[int, str]:
    """Import the torch family in the render env; return (rc, version report)."""
    probe = subprocess.run(
        [sys.executable, "-c",
         "import torch; print('torch', torch.__version__)\n"
         "import torchvision; print('torchvision', torchvision.__version__)\n"
         "import torchaudio; print('torchaudio', torchaudio.__version__)"],
        capture_output=True,
        text=True,
        env=render_env(),
    )
    report = (probe.stdout or probe.stderr or "").strip()
    return probe.returncode, report


def ensure_torch_stack() -> None:
    """Repair torch-family version skew on stock Kaggle images.

    Probes torch + torchvision + torchaudio together in the render
    environment. On failure, reads the installed torch version and repairs
    precisely:
      - torch != TORCH_PIN -> force-reinstall the whole validated set
      - torch == TORCH_PIN -> force-reinstall only the companion pins
        (small wheels; the 2 GB torch wheel is not re-downloaded)
    `--no-deps` keeps pip from touching anything's companions, and the
    post-repair probe re-verifies so a silent no-fix cannot pass unnoticed.
    """
    rc, report = probe_torch_stack()
    if rc == 0:
        print("torch family import OK:")
        for line in report.splitlines():
            print(" ", line)
        return
    print(f"torch family import probe failed:\n  {report}")

    torch_probe = subprocess.run(
        [sys.executable, "-c", "import torch; print(torch.__version__)"],
        capture_output=True, text=True, env=render_env(),
    )
    torch_version = (torch_probe.stdout or "").strip() or "(torch import failed)"
    print(f"installed torch: {torch_version}")

    if TORCH_PIN not in torch_version:
        print(
            f"-> installing the validated set torch=={TORCH_PIN} "
            f"torchvision=={TORCHVISION_PIN} torchaudio=={TORCHAUDIO_PIN}"
        )
        sh([
            sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall",
            f"torch=={TORCH_PIN}",
            f"torchvision=={TORCHVISION_PIN}",
            f"torchaudio=={TORCHAUDIO_PIN}",
            "--index-url", CU121_INDEX,
        ])
    else:
        print(
            f"-> torch is already {TORCH_PIN}; force-reinstalling companions "
            f"torchvision=={TORCHVISION_PIN} torchaudio=={TORCHAUDIO_PIN}"
        )
        sh([
            sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall",
            f"torchvision=={TORCHVISION_PIN}",
            f"torchaudio=={TORCHAUDIO_PIN}",
            "--index-url", CU121_INDEX,
        ])

    rc, report = probe_torch_stack()
    if rc != 0:
        raise RuntimeError(
            "torch/torchvision/torchaudio are still inconsistent after the "
            "repair attempt.\n"
            f"Probe report:\n{report}\n"
            "Paste the FULL cell output back into the chat — the version lines "
            "above plus this report pinpoint the mismatch."
        )
    print("torch family repaired:")
    for line in report.splitlines():
        print(" ", line)


def verify_environment() -> None:
    """Fail fast — with a clear message — if the render deps are missing."""
    print(f"\nkernel python: {sys.executable}")
    # f5_tts.infer.utils_infer is render.py's own import chain end-to-end
    # (torch → torchaudio → transformers → torchvision → f5_tts), so its
    # success is the strongest pre-flight signal we can get without loading
    # the models. Earlier entries exist to give a TARGETED error message if
    # one specific package is the broken link.
    for mod in (
        "torch", "torchvision", "torchaudio", "bigvgan",
        "transformers", "f5_tts.infer.utils_infer",
    ):
        result = subprocess.run(
            [sys.executable, "-c", f"import {mod}; print('{mod} OK')"],
            capture_output=True,
            text=True,
            env=render_env(),
        )
        if result.returncode != 0:
            # Enough stderr to include the actual OSError/ModuleNotFoundError
            # line — earlier 3-line truncation hid the failing .so name.
            tail = (result.stderr or "").strip().splitlines()[-15:]
            raise RuntimeError(
                f"'{mod}' is not importable by the kernel python ({sys.executable}). "
                f"Last errors:\n" + "\n".join(tail)
            )
        print(result.stdout.strip())


def clone_vagdhenu() -> None:
    if (VAGDHENU / ".git").exists():
        print("vagdhenu already cloned, skipping")
        return
    sh(["git", "clone", "--depth", "1", "https://github.com/prathoshap/vagdhenu", str(VAGDHENU)])


def run_setup() -> None:
    # The marker MUST be session-scoped: /kaggle/working persists across
    # Kaggle sessions while installed packages do not. A marker stored next
    # to the clone made fresh sessions skip setup and run against the stock
    # (or half-installed) stack. /tmp dies with the session, so a new session
    # correctly re-runs setup.
    marker = Path(tempfile.gettempdir()) / "vagdhenu_setup_done_v3"
    if marker.exists():
        print("setup already completed in this Kaggle session, skipping")
    else:
        make_interpreter_shim(VAGDHENU / ".shim")
        setup = VAGDHENU / "scripts" / "setup.sh"
        print("--- setup.sh (first 40 lines, for debugging) ---")
        print("\n".join(setup.read_text().splitlines()[:40]))
        print("--- end ---")
        print(
            "NOTE: pip 'dependency resolver' lines below (numpy>=2 conflicts,\n"
            "      torchvision 0.25.0+cu128) are EXPECTED Kaggle noise, not errors.\n"
            "      Vagdhenu pins numpy==1.26.4 by design; the complaining packages\n"
            "      are Kaggle's preinstalled stack, which the render never imports.\n"
        )
        # Installs pinned cu121 torch + deps + BigVGAN + weights, with
        # python/pip forced to the notebook kernel's environment.
        sh("bash scripts/setup.sh", cwd=VAGDHENU)
        marker.touch()
    # Outside the marker guard: the skew repair must also run when setup was
    # already completed in an earlier attempt of this session.
    ensure_torch_stack()
    verify_environment()


def build_shard(workdir: Path) -> Path:
    # no_sandhi is REQUIRED by render.py (it indexes the key per clip; a
    # missing key fails every clip with KeyError 'no_sandhi'). true = our
    # text is already traditionally word-split; skip auto re-splitting.
    #
    # ONE CONTINUOUS PIECE per mantra: padas are space-joined into a single
    # synthesis clip — exactly what the official demo's Renderer.render_one()
    # does for danda-free text. (Per-word padas make render.py stitch 0.55s
    # silences between clips and gate-trim short words to near-nothing.)
    shard = [
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
        for m in MANTRAS
    ]
    shard_path = workdir / "shard.json"
    shard_path.write_text(json.dumps(shard, ensure_ascii=False, indent=2))
    return shard_path


def run_render(workdir: Path) -> None:
    cmd = [
        sys.executable,
        str(VAGDHENU / "src" / "render.py"),
        "--shard", str(workdir / "shard.json"),
        "--results", str(workdir / "results.json"),
        "--outdir", str(workdir),
        # nfe 32 = the demo server's VAGDHENU_NFE default (render.py's own
        # default is 64). Matches the demo's audio and halves render time.
        "--nfe", "32",
    ]
    sh(cmd, cwd=VAGDHENU, env=render_env())  # only Vagdhenu-specific line — see module docstring


def collect_outputs(workdir: Path) -> None:
    audio_dir = CORPUS_OUT / "audio"
    timing_dir = CORPUS_OUT / "timing"
    audio_dir.mkdir(parents=True, exist_ok=True)
    timing_dir.mkdir(parents=True, exist_ok=True)

    for m in MANTRAS:
        produced = workdir / m["audioFileName"]
        if not produced.exists():
            alt = workdir / f"{m['stableID']}_{m['audioFileName']}"
            produced = alt if alt.exists() else produced
        if not produced.exists():
            # Surface the renderer's per-clip results (its error field is
            # exactly why a clip failed) before raising.
            results = workdir / "results.json"
            if results.exists():
                print(f"--- {results.name} ---\n{results.read_text()}")
            raise RuntimeError(
                f"render for {m['stableID']} produced no wav — files in workdir: "
                f"{sorted(p.name for p in workdir.iterdir())}"
            )

        dest = audio_dir / m["audioFileName"]
        shutil.copyfile(produced, dest)
        duration = wav_duration(dest)
        timing = estimate_timing(m["padas"], duration, meter=m["meter"], wav_path=dest)
        (timing_dir / f"{m['stableID']}.json").write_text(
            json.dumps(timing, ensure_ascii=False, indent=2)
        )
        print(f"✓ {m['stableID']}: {m['audioFileName']} ({duration:.2f}s)")


def main() -> None:
    import torch  # preinstalled on Kaggle GPU images

    assert torch.cuda.is_available(), (
        "No GPU visible — set Accelerator to GPU T4 x2 or P100 in notebook options."
    )
    print(f"GPU: {torch.cuda.get_device_name(0)}\n")

    clone_vagdhenu()
    run_setup()

    with tempfile.TemporaryDirectory(prefix="corpus-") as tmp:
        workdir = Path(tmp)
        build_shard(workdir)
        run_render(workdir)
        collect_outputs(workdir)

    zip_path = shutil.make_archive(
        str(WORK / "mantra_corpus"), "zip", root_dir=CORPUS_OUT
    )
    print(f"\nDone. Download from the Output tab: {Path(zip_path).name}")
    print("Unzip into Demo-ios-app/mantras/ so audio/ and timing/ sit alongside mantras.json.")


# Kaggle pastes this file into a cell where __name__ == "__main__", so the
# guard preserves one-shot behavior while keeping the module importable.
if __name__ == "__main__":
    main()
