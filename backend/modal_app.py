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
                "meter": entry["meter"],
                "padas": [" ".join(entry["padas"])],
                "seed": entry.get("seed", 42) if entry.get("seed") is not None else 42,
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
                "seed": seed if seed is not None else 42,
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


# --- Word timings (same rule as scripts/render_corpus.py) ---------------------

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


def estimate_timing(padas: list[str], duration: float, meter: str | None = None) -> list[dict]:
    """Structural timing distribution.

    Mirrors scripts/render_corpus.py and Mantra/Services/TimingEstimator.swift.
    The verse is synthesized as one continuous clip (word-padas space-joined —
    the official demo's render_one() shape), so each word's span is
    proportional to its akshara count across the real WAV duration. Each entry
    spans from its onset to the next onset, so short words are never skipped
    while still sounding. `meter` is accepted for API compatibility; the meter
    cancels out of the math for a single continuous clip.
    """
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
