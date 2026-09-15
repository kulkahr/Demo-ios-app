import Foundation
import SwiftData

/// Persistence layer: corpus seeding, custom chants, favorites, history.
@MainActor
final class MantraRepository {
    private let modelContext: ModelContext
    private let corpusLoader = CorpusLoader()

    init(modelContext: ModelContext) {
        self.modelContext = modelContext
    }

    // MARK: - Seeding (Path A)

    /// Seeds SwiftData from the bundled corpus. Idempotent: existing mantras
    /// (matched by stableID) are updated in place, user flags are preserved.
    func seedIfNeeded() throws {
        let corpus = try corpusLoader.loadCorpus()
        let existing = try fetchAllMantras()
        var byID: [String: Mantra] = [:]
        for mantra in existing where !mantra.isCustom {
            byID[mantra.stableID] = mantra
        }

        for entry in corpus.mantras {
            if let mantra = byID[entry.stableID] {
                mantra.title = entry.title
                mantra.transliteration = entry.transliteration
                mantra.padas = entry.padas
                mantra.meter = entry.meter
                mantra.meaning = entry.meaning
                mantra.sourceName = entry.sourceName
                mantra.audioFileName = entry.audioFileName
            } else {
                let mantra = Mantra(
                    stableID: entry.stableID,
                    title: entry.title,
                    transliteration: entry.transliteration,
                    padas: entry.padas,
                    meter: entry.meter,
                    meaning: entry.meaning,
                    sourceName: entry.sourceName,
                    audioFileName: entry.audioFileName,
                    isCustom: false
                )
                modelContext.insert(mantra)
            }
        }
        try modelContext.save()
    }

    // MARK: - Queries

    func fetchAllMantras() throws -> [Mantra] {
        let descriptor = FetchDescriptor<Mantra>(
            sortBy: [SortDescriptor(\.createdAt, order: .forward)]
        )
        return try modelContext.fetch(descriptor)
    }

    func fetchCorpusMantras() throws -> [Mantra] {
        try fetchAllMantras().filter { !$0.isCustom }
    }

    func fetchCustomMantras() throws -> [Mantra] {
        try fetchAllMantras().filter { $0.isCustom }
    }

    func mantra(withStableID stableID: String) throws -> Mantra? {
        var descriptor = FetchDescriptor<Mantra>(
            predicate: #Predicate { $0.stableID == stableID }
        )
        descriptor.fetchLimit = 1
        return try modelContext.fetch(descriptor).first
    }

    // MARK: - Favorites

    func toggleFavorite(_ mantra: Mantra) throws {
        mantra.isFavorite.toggle()
        try modelContext.save()
    }

    // MARK: - Custom chants (Path B)

    /// Persists a synthesized custom verse along with its cached audio.
    func saveCustomMantra(
        title: String,
        transliteration: String,
        padas: [String],
        meter: String,
        timing: [TimingEntry],
        audio: CachedAudioPayload
    ) throws -> Mantra {
        let id = VagdhenuClient.makeID()
        let audioFileName = "\(id).wav"
        try AudioCache.store(payload: audio, as: audioFileName)

        let mantra = Mantra(
            stableID: id,
            title: title.isEmpty ? Self.defaultTitle(padas: padas) : title,
            transliteration: transliteration,
            padas: padas,
            meter: meter,
            meaning: "",
            sourceName: "Custom chant",
            audioFileName: nil,
            isCustom: true,
            remoteTiming: timing,
            cachedAudio: CachedAudio(fileName: audioFileName, duration: audio.duration)
        )
        modelContext.insert(mantra)
        try modelContext.save()
        return mantra
    }

    func deleteMantra(_ mantra: Mantra) throws {
        // Unlink the file first: after modelContext.delete the relationship is
        // zeroed, so read the name now; deleting the model cascades to
        // CachedAudio, but the on-disk file must be removed explicitly.
        if let fileName = mantra.cachedAudio?.fileName {
            AudioCache.remove(fileName: fileName)
        }
        modelContext.delete(mantra)
        try modelContext.save()
    }

    // MARK: - Session history

    func recordSession(mantra: Mantra, startedAt: Date, loops: Int) throws {
        guard loops > 0 else { return }
        let session = ChantSession(
            startedAt: startedAt,
            completedAt: .now,
            loopsCompleted: loops,
            source: mantra.isCustom ? "custom" : "corpus",
            mantra: mantra
        )
        modelContext.insert(session)
        try modelContext.save()
    }

    func recentSessions(limit: Int = 50) throws -> [ChantSession] {
        var descriptor = FetchDescriptor<ChantSession>(
            sortBy: [SortDescriptor(\.completedAt, order: .reverse)]
        )
        descriptor.fetchLimit = limit
        return try modelContext.fetch(descriptor)
    }

    /// Total loops chanted — the app's headline stat.
    func totalLoopsChanted() throws -> Int {
        let descriptor = FetchDescriptor<ChantSession>()
        return try modelContext.fetch(descriptor).reduce(0) { $0 + $1.loopsCompleted }
    }

    static func defaultTitle(padas: [String]) -> String {
        let first = padas.first.map { String($0.prefix(24)) } ?? "Custom chant"
        return "Custom — \(first)…"
    }
}
