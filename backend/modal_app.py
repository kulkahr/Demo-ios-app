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
import os
import wave
from pathlib import Path

import modal

# --- Container image: Python 3.10 + CUDA 12.1 (Vagdhenu requirements) -------
image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install("git", "ffmpeg", "libsndfile1")
    .pip_install(
        "torch==2.4.0",
        "torchaudio==2.4.0",
        "--index-url", "https://download.pytorch.org/whl/cu121",
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
        """Import Vagdhenu's render pipeline once per container."""
        import sys

        sys.path.insert(0, "/root/vagdhenu")
        os.environ.setdefault("CHAMP_ROOT", "/cache/models")

        # If Vagdhenu's internal API differs, this is the single integration
        # point to adjust.
        from src.render import render_shard  # type: ignore

        self.render_shard = render_shard

    @modal.method()
    def infer(self, entry: dict) -> dict:
        """Run one shard entry through Vagdhenu inside the container."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            shard_path = Path(tmp) / "shard.json"
            shard_path.write_text(json.dumps([entry]))
            results = self.render_shard(
                shard_path=str(shard_path),
                results_path=str(Path(tmp) / "results.json"),
                outdir=tmp,
            )
            # Expect a wav path in results; adapt if Vagdhenu's return shape differs.
            if isinstance(results, dict) and entry["id"] in results:
                wav_path = Path(results[entry["id"]])
            else:
                wav_path = Path(tmp) / entry.get("out", "out.wav")
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
            request_id = str(request.get("id") or "custom-unknown")

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
                "seed": seed if seed is not None else 42,
                # Required by render.py (KeyError per clip if missing); padas
                # arrive already word-split from the client.
                "no_sandhi": True,
                "out": f"{cache_key}.wav",
            }
            result = self.infer.local(entry)

            # --- Word timings (same rule as scripts/render_corpus.py) ------
            timing = estimate_timing(padas, result["duration"], meter=meter)

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


METER_SPS = {
    # sec_per_syll from Vagdhenu's src/reference_bank/bank.json; ASCII stems
    # (render.py's WAV-name keys) plus IAST bank keys.
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
    """Structural timing distribution.

    Mirrors scripts/render_corpus.py and Mantra/Services/TimingEstimator.swift.
    Word onsets follow Vagdhenu's synthesis structure: each pada is a separate
    clip (speech = aksharas × the meter's sec-per-syllable) stitched with a
    fixed gap (0.55s; +0.20s after a virāma-final clip; trailing gap dropped).
    Speech is then rescaled so the plan spans the real WAV. Each entry spans
    from its onset to the next onset, so short words are never skipped while
    still sounding.
    """
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
