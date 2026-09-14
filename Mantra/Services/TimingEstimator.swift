import Foundation

/// Estimates word-level karaoke timings when no measured sidecar exists,
/// distributing audio duration across padas by syllable weight (laghu/guru).
/// Mirrors `scripts/render_corpus.py` so both paths stay consistent.
enum TimingEstimator {
    // Long-vowel matras (guru weight) in Devanagari.
    private static let longMatras: Set<Character> = ["आ", "ई", "ऊ", "ॠ", "ॄ", "ए", "ऐ", "ओ", "औ"]
    // Short-vowel matras (laghu weight).
    private static let shortMatras: Set<Character> = ["अ", "इ", "उ", "ऋ", "ऌ"]
    // Anusvāra/visarga add a mora (counted guru).
    private static let heavyMarks: Set<Character> = ["ं", "ः"]

    static func syllableWeight(_ pada: String) -> Int {
        var weight = 0
        for ch in pada {
            if longMatras.contains(ch) || heavyMarks.contains(ch) {
                weight += 2
            } else if shortMatras.contains(ch) {
                weight += 1
            } else if ch == "्" {
                continue // virāma: part of a consonant cluster, no extra mora
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
