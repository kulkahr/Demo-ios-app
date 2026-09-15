"""Vagdhenu TTS worker for Modal.

Serves the shared contract in docs/api-contract.md:

    POST {BASE_URL}/synthesize
    {"id","meter","padas","seed"?,"text"?} ->
    {"id","audioBase64","duration","timing":[{text,start,end,estimated}...],
     "meter","cached"}

Errors: 400 invalid request · 401 bad API key · 500 inference failure.

Deploy:
    pip install modal
    modal secret create tts-api-key TTS_API_KEY=<your-key>   # optional
    modal deploy backend/modal_app.py

The URL printed by `modal deploy` (https://<workspace>--vagdhenu-tts.modal.run)
is what you paste into the iOS app's Settings screen. The worker clones
prathoshap/vagdhenu and runs its inference pipeline on a GPU container;
weights and the render cache live on a Modal Volume so cold starts skip
re-downloading.

Local testing (requires `pip install fastapi` locally too):
    modal serve backend/modal_app.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import math
import os
import struct
import wave
from pathlib import Path

import modal

# --- Container image: Python 3.10 + CUDA 12.1 (Vagdhenu requirements) -------
image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install("git", "ffmpeg", "libsndfile1")
    # pip flags can't go through pip_install's package list (rejected by
    # current Modal versions), so the cu121 index is set via a shell command.
    .run_commands(
        "pip install torch==2.4.0 torchaudio==2.4.0 "
        "--index-url https://download.pytorch.org/whl/cu121"
    )
    .pip_install("fastapi[standard]", "numpy", "soundfile")
    .run_commands(
        "git clone https://github.com/prathoshap/vagdhenu /root/vagdhenu",
        # Vagdhenu's setup installs remaining deps + BigVGAN; run it last.
        "cd /root/vagdhenu && bash scripts/setup.sh || true",
    )
)

app = modal.App("vagdhenu-tts", image=image)

# Persistent storage: model weights + render cache survive cold starts.
volume = modal.Volume.from_name("vagdhenu-cache", create_if_missing=True)


# GPU: A10G is the cheapest card that comfortably fits the 337M-param DiT
# plus BigVGAN with headroom. Raise to A100 for bulk batch rendering.
@app.cls(
    gpu="A10G",
    volumes={"/cache": volume},
    scaledown_window=300,  # keep warm 5 min between chants
    timeout=600,
)
class VagdhenuTTS:
    @modal.enter()
    def load(self):
        """Verify the Vagdhenu checkout before the first request arrives."""
        self.vagdhenu = Path("/root/vagdhenu")
        render = self.vagdhenu / "src" / "render.py"
        if not render.exists():
            raise RuntimeError(f"Vagdhenu render script missing at {render}")
        # Env for the render subprocess: weights root + BigVGAN on PYTHONPATH.
        # BigVGAN is a cloned repo (not a pip package); render.py's bare
        # `import bigvgan` needs it on the path — same fix the Kaggle script
        # applies.
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in [str(self.vagdhenu / "BigVGAN"), env.get("PYTHONPATH", "")] if p
        )
        env.setdefault("CHAMP_ROOT", "/cache/models")
        self.render_env = env

    @modal.method()
    def infer(self, entry: dict) -> dict:
        """Run one shard entry through Vagdhenu's batch render.py.

        render.py is an argparse batch script (no importable render_shard),
        so the entry goes through a shard file + subprocess — the same
        invocation the corpus scripts use.
        """
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # ONE CONTINUOUS PIECE: join the word-padas into a single
            # synthesis clip — the official demo's Renderer.render_one()
            # renders danda-free text exactly this way. Per-word padas make
            # render.py stitch 0.55s silences between clips and gate-trim
            # short words (ॐ, स्वः, नः) to near-nothing.
            shard_entry = {
                "id": entry["id"],
                "meter": bank_meter_key(entry["meter"], self.vagdhenu),
                "padas": [model_text_from_padas(entry["padas"])],
                # seed 60 = the demo's slider default (and the seed behind the
                # demo's published audio). F5 is seeded-stochastic; seed 42's
                # takes swallowed the leading OM (Kannada ಒಂ) in the Gāyatrī.
                "seed": entry.get("seed", 60) if entry.get("seed") is not None else 60,
                # Required by render.py (KeyError per clip if missing); the
                # text arrives already traditionally word-split.
                "no_sandhi": True,
            }
            shard_path = tmp_path / "shard.json"
            shard_path.write_text(json.dumps([shard_entry], ensure_ascii=False))

            cmd = [
                sys.executable,
                str(self.vagdhenu / "src" / "render.py"),
                "--shard", str(shard_path),
                "--results", str(tmp_path / "results.json"),
                "--outdir", str(tmp_path),  # render.py writes <outdir>/<id>.wav
                # nfe 32 = the demo server's VAGDHENU_NFE default (render.py's
                # own default is 64). Matches the demo's audio.
                "--nfe", "32",
            ]
            subprocess.run(cmd, cwd=self.vagdhenu, check=True, env=self.render_env)

            # render.py exits 0 even when a clip fails (error recorded in
            # results.json) — surface it instead of shipping a missing wav.
            results = json.loads((tmp_path / "results.json").read_text())
            if results and "error" in results[0]:
                raise RuntimeError(f"render failed for {entry['id']}: {results[0]['error']}")

            wav_path = tmp_path / f"{entry['id']}.wav"
            if not wav_path.exists():
                raise RuntimeError(f"render produced no wav for {entry['id']}")
            wav_bytes = wav_path.read_bytes()
            return {"audio": wav_bytes, "duration": _wav_duration(wav_bytes)}

    @modal.asgi_app(label="vagdhenu-tts")
    def api(self):
        """FastAPI app implementing docs/api-contract.md, built in-container."""
        from fastapi import FastAPI, HTTPException, Request

        web_app = FastAPI(title="Vagdhenu TTS")

        @web_app.post("/synthesize")
        async def synthesize(request: dict, http: Request) -> dict:
            # --- Auth (optional): validate against TTS_API_KEY when set. ---
            # compare_digest: constant-time, immune to timing oracles.
            expected_key = os.environ.get("TTS_API_KEY")
            if expected_key:
                auth_header = http.headers.get("authorization", "")
                if not hmac.compare_digest(auth_header, f"Bearer {expected_key}"):
                    raise HTTPException(
                        status_code=401,
                        detail={"code": "unauthorized", "message": "Invalid API key"},
                    )

            # --- Validation ----------------------------------------------
            meter = str(request.get("meter") or "").strip()
            padas = [str(p) for p in (request.get("padas") or []) if str(p).strip()]
            if not meter or not padas:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_request", "message": "meter and non-empty padas are required"},
                )
            seed = request.get("seed")
            # request_id becomes a filename (<outdir>/<id>.wav) — sanitize it.
            request_id = str(request.get("id") or "custom-unknown")
            if not request_id or set(request_id) & set('/\\:\0') or ".." in request_id:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_request", "message": "invalid id"},
                )

            # --- Cache lookup (volume-backed) -----------------------------
            cache_key = hashlib.sha256(
                json.dumps({"m": meter, "p": padas, "s": seed}, sort_keys=True).encode()
            ).hexdigest()
            cache_dir = Path("/cache/render-cache")
            cache_dir.mkdir(parents=True, exist_ok=True)
            wav_cache = cache_dir / f"{cache_key}.wav"
            meta_cache = cache_dir / f"{cache_key}.json"

            if wav_cache.exists() and meta_cache.exists():
                meta = json.loads(meta_cache.read_text())
                return _response(request_id, wav_cache.read_bytes(), meta["timing"], meter, cached=True)

            # --- Inference ------------------------------------------------
            entry = {
                "id": request_id,
                "meter": meter,
                "padas": padas,
                "seed": seed if seed is not None else 60,
            }
            result = self.infer.local(entry)

            # --- Word timings (same rule as scripts/render_corpus.py) ------
            timing = estimate_timing(
                padas, result["duration"], meter=meter, wav_bytes=result["audio"]
            )

            # --- Persist to cache -------------------------------------------
            wav_cache.write_bytes(result["audio"])
            meta_cache.write_text(json.dumps({"duration": result["duration"], "timing": timing}))
            volume.commit()

            return _response(request_id, result["audio"], timing, meter, cached=False)

        return web_app


# --- Helpers -----------------------------------------------------------------


def _response(request_id: str, wav_bytes: bytes, timing: list[dict], meter: str, cached: bool) -> dict:
    return {
        "id": request_id,
        "audioBase64": base64.b64encode(wav_bytes).decode(),
        "duration": _wav_duration(wav_bytes),
        "timing": timing,
        "meter": meter,
        "cached": cached,
    }


def _wav_duration(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        rate = wf.getframerate() or 1
        return wf.getnframes() / float(rate)


# --- Word timings (same rule as scripts/render_corpus.py) ---------------------

VIRAMAS = ("्", "್")  # Devanagari + Kannada virāma


def model_text_from_padas(padas: list[str]) -> str:
    """Space-joined synthesis text for one continuous clip.

    ॐ is kept as-is: it and the long-ō spelling ओं both route to the same
    SLP1 token "oM" (→ Kannada ಒಂ), so respelling cannot change the render.
    The leading OM being swallowed is a sampling issue, not text: F5-TTS is
    seeded-stochastic and some takes drop the short-o hum — seed 60 (the
    demo's slider default) renders it reliably, seed 42 did not.
    """
    return " ".join(padas)


def bank_meter_key(meter: str, vagdhenu_root: Path) -> str:
    """Normalize a meter key to one the reference bank resolves.

    Mirrors scripts/render_corpus.py: the bank's LUT matches its own keys and
    wav stems case-insensitively; a configured name outside it (e.g. "gayatri",
    which the bank does not ship) makes render.py fall back to vasantatilakā
    with a warning per clip. We make that mapping explicit here — same
    reference chant, no per-clip warning, robust if upstream ever errors on
    unknown meters. Unreadable/missing bank: return the meter unchanged.
    """
    bank_path = vagdhenu_root / "src" / "reference_bank" / "bank.json"
    if not bank_path.exists():
        return meter
    try:
        bank = json.loads(bank_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return meter
    lut: set[str] = set()
    for k, v in bank.items():
        if k.startswith("_") or not isinstance(v, dict) or "wav" not in v:
            continue
        lut.add(k.lower())
        lut.add(str(v["wav"]).replace(".wav", "").lower())
    if meter.lower() in lut:
        return meter
    return "vasantatilaka" if "vasantatilaka" in lut else meter


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


def _wav_energy_bytes(wav_bytes: bytes, win_ms: float = 20.0) -> tuple[float, list[float]]:
    """Frame-RMS envelope of an in-memory PCM wav: (duration, rms 0..1)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        rate = wf.getframerate()
        duration = wf.getnframes() / float(rate) if rate else 0.0
        width = wf.getsampwidth()
        data = wf.readframes(wf.getnframes())
    width = max(width, 1)
    n = len(data) // width
    fmt = {1: "B", 2: "h", 4: "i"}.get(width, "h")
    samples = struct.unpack(f"<{n}{fmt}", data[: n * width])
    if width == 1:
        samples = [s - 128 for s in samples]
    win = max(int(rate * win_ms / 1000.0), 1)
    rms = []
    for i in range(0, len(samples), win):
        chunk = samples[i : i + win]
        r = math.sqrt(sum(s * s for s in chunk) / max(len(chunk), 1)) / 32768.0
        rms.append(r)
    return duration, rms


def measure_pauses(
    wav_bytes: bytes,
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

    Same rule as scripts/render_corpus.py's measure_pauses (file-based):
    consecutive frames below `silence_rms` spanning at least `min_silence_s`
    are a pause (leading/trailing `min_gap_s` excluded); each pause is
    attributed to the word whose proportional boundary it follows, minus a
    small `margin_s` so the highlight cuts off just before the breath.
    """
    if not wav_bytes or total_aksharas <= 0 or not padas:
        return []
    try:
        duration, rms = _wav_energy_bytes(wav_bytes, win_ms=win_ms)
    except Exception:
        return []  # undecodable audio: fall back to the uniform plan
    if duration <= 0 or not rms:
        return []
    n = len(padas)
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
    wav_bytes: bytes | None = None,
) -> list[dict]:
    """Phrase-aware structural timing distribution.

    Mirrors scripts/render_corpus.py and Mantra/Services/TimingEstimator.swift
    (the Swift copy is the on-device uniform fallback for custom verses). The
    verse is synthesized as one continuous clip (word-padas space-joined —
    the official demo's render_one() shape), so each word's speech span is
    proportional to its akshara count. When `wav_bytes` is given, the REAL
    silences are measured: speech spans are scaled to the non-silent duration
    (duration − pauses) and each measured pause is appended after its word —
    so onsets after a mid-verse breath move EARLIER (matching the real chant)
    instead of drifting late. Every entry spans from its onset to the next
    onset, so short words are never skipped while still sounding. `meter` is
    accepted for API compatibility; the meter cancels out of the math for a
    single continuous clip.
    """
    n = len(padas)
    if n == 0 or duration <= 0:
        return []
    weights = [akshara_count(p) for p in padas]
    total = sum(weights)

    pauses = measure_pauses(wav_bytes, padas, total) if wav_bytes else []
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
