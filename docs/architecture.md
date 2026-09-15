# Architecture — Vāgdhenu Mantra App

This document is the implementation contract for the iOS mantra-chanting app. It follows the SDLC blueprint in `docs/sdlc.md` (Phases 1–2: scope, stack, architecture). `knowledge.md` requires all code to align with it.

## 1. Product scope (locked)

- **App:** A minimal Sanskrit mantra chanting app that renders mantras with the metered chant TTS from [prathoshap/vagdhenu](https://github.com/prathoshap/vagdhenu) (IISc, Apache-2.0).
- **Delivery: hybrid** — a pre-rendered corpus for the bundled mantra set (offline-first, instant playback), plus an on-demand GPU backend for custom verses.
- **Feature set: Core + karaoke sync** — mantra library, chant playback, word-level karaoke sync highlighting, custom chant synthesis, settings (playback speed, loop count, theme), SwiftData favorites/history.
- **Mantra set: start minimal** — a small curated seed corpus that ships in the bundle, growable via corpus files.

## 2. Stack (per SDLC Phase 2)

| Layer | Choice | Rationale |
|---|---|---|
| Language | Swift 6, strict concurrency | SDLC mandates Swift 6 data-race safety |
| UI framework | SwiftUI | SDLC Phase 2: SwiftUI superseded UIKit |
| Architecture | MVVM | Per `knowledge.md`/SDLC; clean separation of UI, view model, service layer |
| Persistence | SwiftData | SDLC Phase 2: modern persistence for favorites/history |
| Audio | AVFoundation (`AVAudioEngine`) | Sample-accurate karaoke sync via player render callbacks |
| Backend | Modal (serverless GPU) | Python 3.10 + CUDA 12.1 fit Vagdhenu's requirements; pay-per-use; cold starts are tolerable because the corpus path is offline |
| Testing | Swift Testing (unit) + XCUITest (UI) | SDLC Phase 5 mandates this split |

## 3. Hybrid TTS delivery

```
                 ┌──────────────────────────────┐
                 │        iOS app (SwiftUI)     │
                 │  ┌────────────┐ ┌─────────┐  │
                 │  │ MantraList │ │  Chant  │  │
                 │  └─────┬──────┘ └────┬────┘  │
                 │        │  SwiftData  │       │
                 │  ┌─────▼─────────────▼────┐  │
                 │  │        Services        │  │
                 │  │  AudioPlayerService    │  │
                 │  │  VagdhenuClient        │  │
                 │  │  MantraRepository      │  │
                 │  └─────┬─────────┬────────┘  │
                 └────────┼─────────┼───────────┘
                          │         │
   bundled corpus         │         │ HTTPS (shard JSON → WAV + timings)
   mantras/*.json +       │         ▼
   mantras/audio/*.wav ──►│   ┌───────────────────┐
                          │   │ Modal GPU backend │
   on-demand custom verse │   │ FastAPI wrapper   │
   ──────────────────────►│   │ Vagdhenu inference│
                          │   └───────────────────┘
```

### 3.1 Path A — pre-rendered corpus (default, offline)

1. `mantras/mantras.json` + `mantras/audio/*.wav` ship inside the app bundle.
2. `MantraRepository` seeds SwiftData on first launch (`AppModel.seedIfNeeded()`).
3. Playback is fully offline via `AudioPlayerService`.
4. Karaoke timings come from a sidecar `mantras/timing/<mantra_id>.json` if present; otherwise `TimingEstimator` derives them from the WAV duration following the structural model (§5.3), flagged as estimates.

### 3.2 Path B — on-demand synthesis (custom verses)

1. User enters Devanagari text (or IAST) + meter in **Custom Chant**.
2. `VagdhenuClient` builds a **shard entry** mirroring Vagdhenu's format:
   `{"id", "meter", "padas": [...], "seed", "out"}`.
3. POST to the Modal FastAPI endpoint → returns WAV bytes plus word timings.
4. Audio is cached in the app-support directory (repeat chants do not re-hit the GPU), then handed to `AudioPlayerService` with the backend's timing plan.

## 4. Remote TTS API contract

The iOS client and the Modal worker share a JSON contract documented in [api-contract.md](api-contract.md). Both sides implement it; the client treats the endpoint URL as opaque configuration.

## 5. Data model

### 5.1 `Mantra` (SwiftData model)

| Field | Type | Purpose |
|---|---|---|
| `stableID` | String | Deterministic ID (slug) that survives re-seeding |
| `title` | String | Display name (IAST) |
| `transliteration` | String | IAST transliteration shown during chant |
| `padas` | [String] | Devanagari word array — basis of the karaoke timeline |
| `meter` | String | Vagdhenu meter key (e.g. `anushtubh`) |
| `meaning` | String | Short meaning shown on the chant screen |
| `sourceName` | String | Attribution, e.g. "Rigveda 1.1.1" |
| `audioFileName` | String? | Bundle audio for corpus mantras (Path A) |
| `isCustom` | Bool | Path B verses saved by the user |
| `isFavorite` | Bool | SwiftData-backed favorite flag |
| `remoteTiming` | [TimingEntry]? | Word timings for custom verses (Path B), from the backend |

### 5.2 `ChantSession` (SwiftData model)

Recorded after each completed chant for history/stats: `startedAt`, `completedAt`, `loopsCompleted`, relationship to `Mantra`, and `source` (`corpus` or `custom`).

### 5.3 Karaoke sync (core feature)

Word-level sync is the core feature, so its design is explicit:

- **Corpus path with sidecar:** if `mantras/timing/<mantra_id>.json` exists in the bundle it is used directly (produced at render time by the structural estimator below).
- **Corpus path without sidecar:** `TimingEstimator` distributes the WAV duration across padas following the synthesis structure (below); results are flagged as estimates.
- **Custom path:** the Modal backend returns explicit word timings in the synthesis response (api-contract.md); the client uses them directly — no estimation.

**Clip structure (all paths):** Vagdhenu renders the verse as **one continuous clip**: the shard passes the padas space-joined into a single synthesis piece — exactly what the official demo's `Renderer.render_one()` does (it splits pasted text only on dandas/newlines). Passing each pada as its own clip instead makes `render.py` synthesize per-word clips and stitch them with 0.55 s silences (`--gap`), gate-trimming short clips (ॐ, स्वः, नः) to near-nothing — chopped audio. The `meter` still selects the reference chant; unknown names fall back to `vasantatilakā` by design.

**Structural timing model (all paths):** within one continuous clip the chant pace is steady, so each word's span is **proportional to its akshara count** across the real WAV duration, and every entry spans from its onset to the **next** onset so a short word is never skipped while still sounding. (With no inter-word gaps the meter's `sec_per_syllable` and rescaling cancel out of the math.)

**Phrase pauses (corpus + worker paths):** real chants breathe — a half-verse pause of 0.5–0.9 s would otherwise push every later highlight late by an accumulating drift. When the rendered WAV is available, `measure_pauses` scans its frame energy and attributes each silence ≥0.30 s to the word before it (minus a 30 ms margin so the highlight cuts off just before the breath); speech spans are then scaled to the non-silent duration and each measured pause is appended after its word, so post-pause onsets track the real audio instead of drifting. With no WAV (or an unmeasurable one) the plan falls back to pure uniform. The Swift on-device fallback stays uniform — it runs before audio exists. Implemented identically in `scripts/render_corpus.py`, `scripts/kaggle_render.py` and `backend/modal_app.py` — changes must be applied to all four.

**OM in model text:** `ॐ` is kept as-is — it and the long-ō spelling `ओं` both route to the same SLP1 token `oM` (→ Kannada ಒಂ), so respelling cannot change the render. A swallowed leading OM is a **sampling** issue: F5-TTS is seeded-stochastic and some takes drop the short-o hum. All render paths use **seed 60 / nfe 32** — the demo server's defaults, whose takes render the OM reliably (seed 42 / nfe 64 did not). Changing the seed changes the take deterministically; a re-render with the same seed reproduces byte-identical audio.

### 5.4 Services

| Service | Responsibility |
|---|---|
| `AudioPlayerService` | AVAudioEngine playback of local WAVs, loop count, speed, karaoke position callbacks |
| `MantraRepository` | SwiftData seeding from `mantras.json`, CRUD for custom chants, favorites, session history |
| `VagdhenuClient` | HTTPS client for the Modal endpoint, shard-JSON builder |
| `TimingEstimator` | Structural word timings (akshara-proportional) when no sidecar exists |

## 6. Directory layout

```
Mantra/
  App/                — MantraApp.swift, RootView, AppModel
  Models/             — Mantra.swift, ChantSession.swift, TimingEntry.swift
  Services/           — AudioPlayerService, VagdhenuClient, MantraRepository,
                        CorpusLoader, TimingEstimator, KeychainStore
  ViewModels/         — ChantViewModel, CustomChantViewModel, AppSettings
  Views/              — MantraListView, ChantView, CustomChantView, SettingsView
  Tests/              — Swift Testing suite
backend/
  modal_app.py        — Modal app definition (FastAPI + Vagdhenu inference)
scripts/
  render_corpus.py    — batch-render mantras.json via Vagdhenu → mantras/audio
mantras/
  mantras.json        — seed corpus
  audio/              — rendered WAVs (generated, not committed)
  timing/             — sidecar word timings (generated, not committed)
```

The `mantras/` directory ships as a **folder reference** in `project.yml` and
is bundled verbatim (no manual Xcode step); `Mantra/Tests/` is excluded from
the app target and compiled only into the `MantraTests` bundle.

## 7. Consistency rules

- **Corpus rendering:** `scripts/render_corpus.py` reads `mantras/mantras.json`, calls Vagdhenu's `src/render.py`, and emits WAVs + sidecar timing JSONs. It must only run on a CUDA 12.1 machine after `scripts/setup.sh` in the Vagdhenu repo — never in this sandbox.
- **Shared contract:** backend and client implement the same shard-JSON + response contract (api-contract.md); both are complete and must not drift.
- **No hard-coded URLs or secrets:** the TTS endpoint is stored in `UserDefaults`; the API key is stored in the **Keychain** (`KeychainStore`) and migrated there from legacy `UserDefaults` storage on first launch. An unconfigured endpoint degrades the UI gracefully to corpus-only mode.
- **No placeholders:** every code path must be complete; there are no `// TODO` stubs.
- **Generated artifacts are not committed:** WAV/timing files are reproducible from `mantras.json` + the render script (or `scripts/regen_timing.py` for sidecars only, no GPU needed); the JSON is the source of truth.
- **Meter names resolve against Vagdhenu's reference bank:** `render.py` looks up the `meter` field by bank key (IAST, e.g. `anuṣṭubh`) or WAV stem (ASCII, e.g. `anushtubh`). Supported stems: `anushtubh`, `vasantatilaka`, `upajati`, `indravajra`, `upendravajra`, `vamshastha`, `rathoddhata`, `shalini`, `indravamsha`, `drutavilambita`, `bhujangaprayata`, `malini`, `shardulavikridita`, `sragdhara`, `pramanika`, `vrutta1`. A name outside the bank (e.g. `gayatri`) is not an error — Vagdhenu falls back to `vasantatilaka` by design (logged as `[meter] unknown vṛtta … -> fallback`). New corpus entries should prefer a bank-resolvable name; if a non-bank meter is kept for correctness of the data model, expect the fallback contour.
