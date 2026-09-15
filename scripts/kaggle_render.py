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
import os
import shutil
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

# --- Timing estimator: mirrors scripts/render_corpus.py on your Mac
# (structural model: per-pada speech = aksharas × meter sec-per-syllable +
# fixed inter-pada gaps, rescaled to the WAV duration; keep all
# implementations in parity — TimingEstimator.swift, backend/modal_app.py).

METER_SPS = {
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


def meter_sps(meter: str | None) -> float:
    return METER_SPS.get((meter or "").strip().lower(), DEFAULT_SPS)


def estimate_timing(padas: list[str], duration: float, meter: str | None = None) -> list[dict]:
    """Structural karaoke timings: onsets follow Vagdhenu's synthesis
    structure (per-pada speech + fixed inter-pada gaps, rescaled to the real
    WAV duration). Each entry spans from its speech onset to the next entry's
    onset, so a word stays highlighted through the pause after it — short
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
    # padas are already traditionally word-split; skip auto re-splitting —
    # mirrors Vagdhenu's own production shard example.
    shard = [
        {
            "id": m["stableID"],
            "meter": m["meter"],
            "padas": m["padas"],
            "seed": 42,
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
        timing = estimate_timing(m["padas"], duration, meter=m["meter"])
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
