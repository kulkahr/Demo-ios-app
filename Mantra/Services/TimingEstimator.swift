import Foundation

/// Estimates word-level karaoke timings when no measured sidecar exists.
///
/// Structural model mirroring Vagdhenu's synthesis: each pada is rendered as
/// its own clip (speech = aksharas × the meter's sec-per-syllable) stitched
/// with a fixed inter-pada gap (0.55 s; +0.20 s after a virāma-final clip;
/// the trailing gap is dropped). Speech is then globally rescaled so the plan
/// spans the real audio — the gaps are exact digital silence, and the
/// residual (gate/F5 trim) scales with speech.
///
/// Each entry spans from its speech onset to the **next** entry's onset, so a
/// word stays highlighted through the pause after it and the next word lights
/// up exactly at its onset — short words (ॐ, यो, नः, मा) are never skipped
/// while still sounding.
///
/// PARITY: scripts/render_corpus.py, scripts/kaggle_render.py and
/// backend/modal_app.py implement the same rule (architecture.md §5.3).
/// Weighting is per akshara (syllable), ported from render.py's n_aksharas —
/// not per grapheme cluster or mora.
enum TimingEstimator {
    /// sec-per-syllable from Vagdhenu's src/reference_bank/bank.json; ASCII
    /// stems (render.py's WAV-name keys) plus IAST bank keys.
    static let meterSecPerSyllable: [String: Double] = [
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
    ]
    /// render.py's unknown-meter fallback is vasantatilakā.
    static let defaultSecPerSyllable = 0.259
    /// render.py --gap.
    static let gapSeconds = 0.55
    /// render.py --gap_halant (added after a virāma-final clip).
    static let gapHalantSeconds = 0.20
    /// Devanagari + Kannada virāma.
    private static let viramas: Set<Unicode.Scalar> = ["\u{094D}", "\u{0CCD}"]

    /// Syllables (aksharas) in a pada — port of render.py's n_aksharas.
    /// Independent vowels and non-halant consonants count 1; a consonant
    /// followed by virāma starts a cluster and adds nothing; ॐ chants as one
    /// syllable; matras, anusvāra, visarga, joiners and dandas add nothing.
    static func aksharaCount(_ pada: String) -> Int {
        let scalars = Array(pada.unicodeScalars)
        var count = 0
        for (i, scalar) in scalars.enumerated() {
            let v = scalar.value
            if (0x0905...0x0914).contains(v) || (0x0C85...0x0C94).contains(v) {
                count += 1  // independent vowels
            } else if (0x0915...0x0939).contains(v) || (0x0C95...0x0CB9).contains(v) {
                let next = i + 1 < scalars.count ? scalars[i + 1] : nil
                if next.map({ !viramas.contains($0) }) ?? true {
                    count += 1  // consonant onset (halant consonants join the next)
                }
            } else if v == 0x0950 {
                count += 1  // ॐ
            }
        }
        return max(count, 1)
    }

    static func secPerSyllable(for meter: String?) -> Double {
        guard let meter else { return defaultSecPerSyllable }
        let key = meter.trimmingCharacters(in: .whitespaces).lowercased()
        return meterSecPerSyllable[key] ?? defaultSecPerSyllable
    }

    /// Distributes `duration` across `padas` following the synthesis
    /// structure (speech + gaps, rescaled). Pass the mantra's `meter` when
    /// known — it selects the meter's reference speech rate.
    static func estimate(padas: [String], duration: Double, meter: String? = nil) -> TimingPlan {
        guard !padas.isEmpty, duration > 0 else { return TimingPlan(entries: []) }
        let sps = secPerSyllable(for: meter)
        let n = padas.count
        var speech = padas.map { Double(aksharaCount($0)) * sps }
        let gaps = padas.map { pada -> Double in
            let halant = pada.unicodeScalars.last.map { viramas.contains($0) } ?? false
            return gapSeconds + (halant ? gapHalantSeconds : 0)
        }
        let gapTotal = gaps.dropLast().reduce(0, +)  // stitcher drops the trailing gap
        let speechTotal = speech.reduce(0, +)
        if speechTotal > 0, duration > gapTotal {
            let k = (duration - gapTotal) / speechTotal
            speech = speech.map { $0 * k }
        }

        var onsets: [Double] = []
        var cursor = 0.0
        for (i, s) in speech.enumerated() {
            onsets.append(cursor)
            cursor += s + (i < n - 1 ? gaps[i] : 0)
        }

        var entries: [TimingEntry] = []
        for (i, pada) in padas.enumerated() {
            let start = min(onsets[i], duration)
            let end = min(i + 1 < n ? onsets[i + 1] : duration, duration)
            entries.append(TimingEntry(text: pada, start: start, end: end, estimated: true))
        }
        return TimingPlan(entries: entries)
    }
}
