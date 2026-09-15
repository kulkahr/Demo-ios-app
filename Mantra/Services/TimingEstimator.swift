import Foundation

/// Estimates word-level karaoke timings when no measured sidecar exists.
///
/// Structural model mirroring Vagdhenu's synthesis: the verse is synthesized
/// as ONE CONTINUOUS clip — the shard passes the padas space-joined into a
/// single piece, exactly like the official demo's `Renderer.render_one()`,
/// which splits text only on dandas/newlines. (Passing each pada as its own
/// clip instead makes render.py stitch per-word clips with 0.55 s silences
/// and gate-trim short words like ॐ/स्वः/नः down to near-nothing — chopped
/// audio.) Within one continuous clip the chant pace is steady, so each
/// word's span is proportional to its akshara count across the real audio
/// duration — the meter's sec-per-syllable and any rescaling cancel out of
/// the math.
///
/// Each entry spans from its onset to the **next** entry's onset, so a word
/// stays highlighted until the next one sounds — short words (ॐ, यो, नः, मा)
/// are never skipped while still sounding.
///
/// PARITY: scripts/render_corpus.py, scripts/kaggle_render.py and
/// backend/modal_app.py implement the same rule (architecture.md §5.3).
/// Weighting is per akshara (syllable), ported from render.py's n_aksharas —
/// not per grapheme cluster or mora.
enum TimingEstimator {
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
                // Virāma (Devanagari 094D / Kannada 0CCD) joins the cluster.
                if next.map({ $0.value != 0x094D && $0.value != 0x0CCD }) ?? true {
                    count += 1  // consonant onset (halant consonants join the next)
                }
            } else if v == 0x0950 {
                count += 1  // ॐ
            }
        }
        return max(count, 1)
    }

    /// Distributes `duration` across `padas` proportionally to akshara
    /// counts. `meter` is accepted for API compatibility with the per-clip
    /// gap model this replaces; pacing is uniform within a single continuous
    /// clip, so the meter cancels out of the math.
    static func estimate(padas: [String], duration: Double, meter: String? = nil) -> TimingPlan {
        guard !padas.isEmpty, duration > 0 else { return TimingPlan(entries: []) }
        let weights = padas.map { Double(aksharaCount($0)) }
        let total = weights.reduce(0, +)

        var entries: [TimingEntry] = []
        var cumulative = 0.0
        for (i, pada) in padas.enumerated() {
            let start = duration * cumulative / total
            cumulative += weights[i]
            let end = i + 1 < padas.count ? duration * cumulative / total : duration
            entries.append(TimingEntry(text: pada, start: start, end: end, estimated: true))
        }
        return TimingPlan(entries: entries)
    }
}
