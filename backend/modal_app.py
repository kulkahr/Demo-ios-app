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
            timing = estimate_timing(padas, result["duration"])

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


def estimate_timing(padas: list[str], duration: float) -> list[dict]:
    """Syllable-weight timing distribution.

    Mirrors scripts/render_corpus.py and Mantra/Services/TimingEstimator.swift.
    Weights are assigned per code point — NOT per grapheme cluster — because
    dependent matra signs combine into clusters with their consonant and would
    otherwise be invisible to the weighting.
    """
    # Devanagari characters are written literally; this file is UTF-8.
    LONG = set(
        "आईऊॠॡएऐओऔ"  # independent long vowels
        "ाीूॄेैोौ"  # dependent matra signs: ā ī ū ṝ e ai o au
    )
    SHORT = set(
        "अइउऋऌ"  # independent short vowels
        "िुृॢ"  # dependent signs: i u ṛ ḷ
    )
    HEAVY = set("ँंः")  # candrabindu, anusvāra, visarga → guru
    SKIP = set("्\u200c\u200d।॥")  # virāma, ZWNJ/ZWJ, dandas
    VIRAMA = "्"

    def weight(pada: str) -> int:
        w = 0
        for ch in pada:
            if ch in LONG or ch in HEAVY:
                w += 2
            elif ch in SHORT:
                w += 1
            elif ch == VIRAMA or ch in SKIP:
                continue
            else:
                w += 1
        return max(w, 1)

    weights = [weight(p) for p in padas]
    total = sum(weights)
    timing: list[dict] = []
    cursor = 0.0
    for pada, w in zip(padas, weights):
        end = min(cursor + duration * (w / total), duration)
        timing.append(
            {"text": pada, "start": round(cursor, 3), "end": round(end, 3), "estimated": True}
        )
        cursor = end
    # Snap the final boundary exactly to the duration.
    if timing:
        timing[-1]["end"] = round(duration, 3)
    return timing
