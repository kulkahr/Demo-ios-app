import Foundation

/// Estimates word-level karaoke timings when no measured sidecar exists,
/// distributing audio duration across padas by syllable weight (laghu/guru).
///
/// Weights are assigned per **Unicode scalar** — not per grapheme cluster —
/// because Devanagari matra signs combine into clusters with their consonant
/// and would otherwise be invisible. This mirrors the Python implementations
/// in `scripts/render_corpus.py` and `backend/modal_app.py`; any change here
/// must be applied to both (Swift ↔ Python parity, architecture.md §5.3).
enum TimingEstimator {
    // Long vowels (guru, 2 moras) as dependent matra signs and independent letters.
    private static let longScalars: Set<Unicode.Scalar> = [
        "\u{093E}", "\u{0940}", "\u{0942}", "\u{0944}", // ā ī ū ṝ signs
        "\u{0947}", "\u{0948}", "\u{094A}", "\u{094B}", // e ai o au signs (guru in Sanskrit)
        "\u{0906}", "\u{0908}", "\u{090A}", "\u{0960}", "\u{0961}", // Ā Ī Ū Ǖ 噜 independent
        "\u{090F}", "\u{0910}", "\u{0913}", "\u{0914}", // Ē AI Ō AU independent
    ]
    // Short vowels (laghu, 1 mora).
    private static let shortScalars: Set<Unicode.Scalar> = [
        "\u{093F}", "\u{0941}", "\u{0943}", "\u{0962}", // i u ṛ ḷ signs
        "\u{0945}", "\u{0946}", // candra e / short e signs (rare)
        "\u{0905}", "\u{0907}", "\u{0909}", "\u{090B}", "\u{090C}", // A I U R̤ L̤ independent
    ]
    // Anusvāra/candrabindu/visarga make the syllable guru (2 moras).
    private static let heavyScalars: Set<Unicode.Scalar> = ["\u{0901}", "\u{0902}", "\u{0903}"]
    // Virāma: part of a consonant cluster, no extra mora.
    private static let virama: Unicode.Scalar = "\u{094D}"
    // Joiners/non-joiners carry no prosodic weight.
    private static let invisibleScalars: Set<Unicode.Scalar> = ["\u{200C}", "\u{200D}"]
    // Dandas are punctuation, not chanted material.
    private static let punctuationScalars: Set<Unicode.Scalar> = ["\u{0964}", "\u{0965}"]

    static func syllableWeight(_ pada: String) -> Int {
        var weight = 0
        for scalar in pada.unicodeScalars {
            if longScalars.contains(scalar) || heavyScalars.contains(scalar) {
                weight += 2
            } else if shortScalars.contains(scalar) {
                weight += 1
            } else if scalar == virama
                        || invisibleScalars.contains(scalar)
                        || punctuationScalars.contains(scalar) {
                continue
            } else {
                weight += 1
            }
        }
        return max(weight, 1)
    }

    /// Distributes `duration` across `padas`, weighted by syllable weight.
    static func estimate(padas: [String], duration: Double) -> TimingPlan {
        guard !padas.isEmpty, duration > 0 else { return TimingPlan(entries: []) }
        let weights = padas.map(syllableWeight)
        let total = weights.reduce(0, +)
        var cursor = 0.0
        var entries: [TimingEntry] = []
        for (pada, w) in zip(padas, weights) {
            let span = duration * (Double(w) / Double(total))
            let end = min(cursor + span, duration)
            entries.append(
                TimingEntry(text: pada, start: cursor, end: end, estimated: true)
            )
            cursor = end
        }
        // Snap the final boundary exactly to the duration.
        if !entries.isEmpty {
            let last = entries.removeLast()
            entries.append(
                TimingEntry(text: last.text, start: last.start, end: duration, estimated: true)
            )
        }
        return TimingPlan(entries: entries)
    }
}
