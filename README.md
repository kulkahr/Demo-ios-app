# Vāgdhenu Mantra

A minimal iOS app for chanting Sanskrit mantras, rendered with the metered chant TTS from [prathoshap/vagdhenu](https://github.com/prathoshap/vagdhenu) (IISc, Apache-2.0). Mantras are chanted with metrically-aware durations and tradition-faithful melodic contour — not flat read-aloud — with **word-level karaoke sync**.

## Architecture: hybrid TTS delivery

| Path | What | When |
|---|---|---|
| **A — Corpus (offline)** | Mantras pre-rendered by Vagdhenu ship in the app bundle (`mantras/mantras.json` + WAVs) | Default. Instant, offline playback of the curated set |
| **B — On-demand (GPU)** | Custom verses are synthesized via a Modal serverless GPU worker | Custom Chant tab; needs a configured endpoint |

Full details: [`docs/architecture.md`](docs/architecture.md) · API contract: [`docs/api-contract.md`](docs/api-contract.md) · Process: [`docs/sdlc.md`](docs/sdlc.md)

## Repository layout

```
Mantra/               iOS app (Swift 6 · SwiftUI · MVVM · SwiftData)
  App/                Entry point, root tabs, seeding
  Models/             Mantra, ChantSession, TimingEntry
  Services/           AudioPlayer, VagdhenuClient, Repository, TimingEstimator
  ViewModels/         Chant, CustomChant, AppSettings
  Views/              Library, karaoke Chant, Custom Chant, Settings
  Tests/              Swift Testing suite
backend/
  modal_app.py        Modal GPU worker (FastAPI + Vagdhenu inference)
mantras/
  mantras.json        Seed corpus (source of truth)
  audio/              Rendered WAVs (generated — not committed)
  timing/             Karaoke sidecars (generated — not committed)
scripts/
  render_corpus.py    Batch-render the corpus via Vagdhenu (needs CUDA)
docs/                 SDLC blueprint + architecture + API contract
```

## Getting started (iOS app)

1. **Render the corpus** on a CUDA 12.1 machine (one-time):
   ```bash
   git clone https://github.com/prathoshap/vagdhenu ~/vagdhenu
   cd ~/vagdhenu && bash scripts/setup.sh    # deps + weights
   cd <this repo>
   VAGDHENU_ROOT=~/vagdhenu python3 scripts/render_corpus.py
   ```
   This fills `mantras/audio/` and `mantras/timing/`. `mantras/` is bundled
   automatically as a folder reference — no manual Xcode step needed.

2. **Generate the Xcode project**:
   ```bash
   brew install xcodegen
   xcodegen generate
   open Mantra.xcodeproj
   ```

3. **Run**: build the `Mantra` scheme. The library works fully offline once the corpus is bundled.

4. **Enable custom chants (Path B)** — deploy the worker:
   ```bash
   pip install modal
   modal secret create tts-api-key TTS_API_KEY=<your-key>   # optional
   modal deploy backend/modal_app.py
   ```
   Paste the printed URL into **Settings → TTS endpoint** in the app (plus the API key if you set one — it is stored in the device Keychain). Without it, the app runs in corpus-only mode.

## Karaoke sync

Word timings resolve in priority order (see `docs/architecture.md` §5.3):

1. Backend-provided timings (custom verses)
2. Sidecar `mantras/timing/<id>.json` (corpus, produced at render time)
3. On-device structural estimation (`TimingEstimator`) — flagged as estimated

The verse is rendered as one continuous clip (padas space-joined in the shard — the same shape as Vagdhenu's official demo), so the estimator distributes the audio duration across padas proportionally to each word's akshara (syllable) count: short words are never skipped and highlights switch exactly at word onsets. At corpus-render time the real phrase pauses (breaths) are measured from the WAV's energy envelope and appended after their word, so highlights after a mid-verse pause stay in sync instead of drifting late; the on-device fallback (before audio exists) stays uniform. The same algorithm is implemented in Swift, Python (worker + both render scripts) so all paths agree. The model text also spells OM as ओं (long-ō) — the frontend's default short-o mapping can make the model drop it. Rebuilt corpus sidecars after changing the estimator with:

```bash
python3 scripts/regen_timing.py   # no GPU needed — reads existing WAVs
```

## Attributions & licenses

- Vagdhenu — Prof. Prathosh, IISc Bengaluru, Apache-2.0. Voice is the author's own; intended for pārāyaṇa/study/accessibility — use responsibly, do not impersonate.
- Built on AI4Bharat IndicF5 (MIT), NVIDIA BigVGAN-v2, F5-TTS.
- Vagdhenu weights are downloaded per the HF model card terms when you run its setup script.
