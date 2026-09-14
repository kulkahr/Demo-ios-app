# Vagdhenu TTS API Contract

Shared contract between the iOS app (`VagdhenuClient`) and the Modal GPU worker (`backend/modal_app.py`). Both sides must implement exactly this shape; changes require updating both together.

## Endpoint

```
POST {BASE_URL}/synthesize
Content-Type: application/json
```

`BASE_URL` is user-configured (stored in `AppStorage("ttsEndpoint")`), never hard-coded. Expected form: `https://<workspace>--vagdhenu-tts-serve.modal.run`.

### Request body — shard entry

Mirrors Vagdhenu's batch shard JSON format (`src/render.py --shard`):

```json
{
  "id": "custom-1726000000",
  "meter": "anushtubh",
  "padas": ["ॐ", "भूर्भुवः", "स्वः"],
  "seed": 42,
  "text": "ॐ भूर्भुवः स्वः"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Client-generated unique ID, also used as the cache key |
| `meter` | string | yes | Vagdhenu meter key, e.g. `anushtubh` |
| `padas` | [string] | yes | Devanagari words, in chant order |
| `seed` | int? | no | Optional determinism seed |
| `text` | string | no | Full verse string (used by worker-side sandhi/frontend when padas need re-splitting) |

### Response — success (200)

```json
{
  "id": "custom-1726000000",
  "audioBase64": "<U8cmV3Li4u base64 WAV bytes>",
  "duration": 6.421,
  "timing": [
    {"text": "ॐ",     "start": 0.0,   "end": 0.92,  "estimated": false},
    {"text": "भूर्भुवः", "start": 0.92,  "end": 2.41,  "estimated": false},
    {"text": "स्वः",   "start": 2.41,  "end": 3.30,  "estimated": false}
  ],
  "meter": "anushtubh",
  "cached": false
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | string | Echoes the request ID |
| `audioBase64` | string | WAV (PCM 16-bit, 24 kHz — BigVGAN output rate) |
| `duration` | double | Audio duration in seconds |
| `timing[].text` | string | The pada (word) as sent |
| `timing[].start/end` | double | Seconds, monotonically non-decreasing, `end` of last ≤ `duration` |
| `timing[].estimated` | bool | `true` if the worker could not extract exact word boundaries and estimated them — the client displays identical behavior for either value |
| `meter` | string | Echoed meter |
| `cached` | bool | `true` if served from the worker's volume cache |

### Errors

| Status | Meaning | Client behavior |
|---|---|---|
| 400 | Invalid meter / empty padas | Show validation message before retry |
| 401 | Missing/invalid API key | Prompt to check the key in Settings |
| 429 | Rate limited | Show retry-with-backoff message |
| 500 | Inference failure | Show generic failure; audio cache untouched |
| 503 | Model loading (cold start) | Auto-retry up to 2× after 5 s |

All error bodies: `{"error": {"code": string, "message": string}}`.

## Auth

If the user has set an API key (`AppStorage("ttsApiKey")`), the client sends:

```
Authorization: Bearer <key>
```

The worker validates it against `TTS_API_KEY` (Modal secret). If `TTS_API_KEY` is unset on the worker, requests are accepted without a key (development mode).

## Worker-side caching

The worker keeps a Modal Volume cache keyed by `(meter, joined padas, seed)`. A cache hit returns `cached: true` with the same response shape. This mirrors the client-side cache; either layer may serve a repeat request.

## Timing rules

- `timing` must contain exactly one entry per pada, in order.
- Word boundaries come from the render pipeline when available; otherwise the worker estimates them the same way `TimingEstimator` does on-device (syllable-weight distribution), marking `estimated: true`.
- Clients must clamp: if `end > duration`, clamp to `duration`.
