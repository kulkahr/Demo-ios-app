import Foundation
import SwiftData

/// A chantable mantra. Corpus mantras are seeded from `mantras/mantras.json`;
/// custom verses are saved by the user via the on-demand TTS path.
@Model
final class Mantra {
    /// Deterministic ID (slug) that survives re-seeding, e.g. "gayatri".
    @Attribute(.unique) var stableID: String
    var title: String
    var transliteration: String
    /// Devanagari words in chant order — the karaoke timeline basis.
    var padas: [String]
    var meter: String
    var meaning: String
    var sourceName: String
    /// Bundle audio file name for corpus mantras (Path A); nil for custom verses.
    var audioFileName: String?
    var isCustom: Bool
    var isFavorite: Bool
    var createdAt: Date
    /// JSON-encoded word timings for custom verses (Path B), from the TTS backend.
    @Attribute(.externalStorage) private var remoteTimingData: Data?

    /// Audio cached on-device after a remote synthesis (custom verses).
    @Relationship(deleteRule: .cascade) var cachedAudio: CachedAudio?

    init(
        stableID: String,
        title: String,
        transliteration: String,
        padas: [String],
        meter: String,
        meaning: String,
        sourceName: String,
        audioFileName: String? = nil,
        isCustom: Bool = false,
        isFavorite: Bool = false,
        createdAt: Date = .now,
        remoteTiming: [TimingEntry]? = nil,
        cachedAudio: CachedAudio? = nil
    ) {
        self.stableID = stableID
        self.title = title
        self.transliteration = transliteration
        self.padas = padas
        self.meter = meter
        self.meaning = meaning
        self.sourceName = sourceName
        self.audioFileName = audioFileName
        self.isCustom = isCustom
        self.isFavorite = isFavorite
        self.createdAt = createdAt
        self.setRemoteTiming(remoteTiming)
        self.cachedAudio = cachedAudio
    }

    // MARK: - Remote timing accessors

    /// Word timings from the backend (custom verses), decoded on access.
    var remoteTiming: [TimingEntry]? {
        get {
            guard let remoteTimingData else { return nil }
            return try? JSONDecoder().decode([TimingEntry].self, from: remoteTimingData)
        }
        set { setRemoteTiming(newValue) }
    }

    private func setRemoteTiming(_ entries: [TimingEntry]?) {
        guard let entries else {
            remoteTimingData = nil
            return
        }
        remoteTimingData = try? JSONEncoder().encode(entries)
    }
}

/// Locally cached WAV for a custom (Path B) mantra.
@Model
final class CachedAudio {
    /// File name inside the app-support audio cache directory.
    var fileName: String
    var duration: TimeInterval
    var createdAt: Date

    init(fileName: String, duration: TimeInterval, createdAt: Date = .now) {
        self.fileName = fileName
        self.duration = duration
        self.createdAt = createdAt
    }
}
