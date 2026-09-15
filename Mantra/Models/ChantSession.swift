import Foundation
import SwiftData

/// A completed chant, recorded for history and stats.
@Model
final class ChantSession {
    var startedAt: Date
    var completedAt: Date
    var loopsCompleted: Int
    /// "corpus" or "custom" — mirrors Mantra.isCustom at session time.
    var source: String
    @Relationship(deleteRule: .nullify) var mantra: Mantra?

    init(
        startedAt: Date,
        completedAt: Date = .now,
        loopsCompleted: Int,
        source: String,
        mantra: Mantra?
    ) {
        self.startedAt = startedAt
        self.completedAt = completedAt
        self.loopsCompleted = loopsCompleted
        self.source = source
        self.mantra = mantra
    }
}
