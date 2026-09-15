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
    func entry(at position: Double) -> TimingEntry? {
        entries.first { position >= $0.start && position < $0.end }
    }

    /// Index of the active entry, or nil between/after entries.
    func index(at position: Double) -> Int? {
        entries.firstIndex { position >= $0.start && position < $0.end }
    }
}
