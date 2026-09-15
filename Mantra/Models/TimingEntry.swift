import Foundation

/// One word (pada) of a karaoke timing plan.
/// Codable so it can round-trip through the TTS API contract.
struct TimingEntry: Codable, Hashable, Sendable {
    let text: String
    let start: Double
    let end: Double
    /// True when boundaries were estimated rather than measured.
    let estimated: Bool

    var duration: Double { end - start }
}

/// A full word-timing plan for a chant.
struct TimingPlan: Sendable, Equatable {
    let entries: [TimingEntry]

    /// Entry containing the given playback position, if any.
    ///
    /// The FINAL entry is open-ended at its `end`: our generators pin the
    /// last entry's end to the audio duration, so positions in the tail
    /// (the finish callback's exact-duration tick, or a poll that lands
    /// after the last scheduled frame) must still resolve to the last word
    /// — otherwise the final word can visually never highlight.
    func entry(at position: Double) -> TimingEntry? {
        guard let i = index(at: position) else { return nil }
        return entries[i]
    }

    /// Index of the active entry, or nil between/after entries.
    func index(at position: Double) -> Int? {
        for (i, e) in entries.enumerated() {
            if position >= e.start && position < e.end {
                return i
            }
        }
        if let last = entries.last, position >= last.start, position <= last.end {
            return entries.count - 1
        }
        return nil
    }
}
