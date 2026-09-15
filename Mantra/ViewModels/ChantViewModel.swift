import Combine
import Foundation
import SwiftData

/// Drives the karaoke chant screen: playback state, word highlighting,
/// loop counts, and session recording. Owned by `ChantView`.
@MainActor
final class ChantViewModel: ObservableObject {
    // MARK: - Published UI state

    @Published private(set) var timingPlan: TimingPlan = TimingPlan(entries: [])
    @Published private(set) var activeWordIndex: Int? = nil
    @Published private(set) var activeWordText: String = ""
    @Published var errorMessage: String? = nil

    let audioService: AudioPlayerService

    private var mantra: Mantra?
    private var modelContext: ModelContext?
    private var sessionStart: Date?
    private var loopsCompletedThisSession = 0
    private var sessionRecorded = false
    private var cancellables = Set<AnyCancellable>()

    init(audioService: AudioPlayerService = AudioPlayerService()) {
        self.audioService = audioService

        // Track the active word from playback position at ~30 fps.
        audioService.$currentTime
            .receive(on: RunLoop.main)
            .sink { [weak self] position in
                self?.updateActiveWord(position: position)
            }
            .store(in: &cancellables)

        // Count completed loops.
        audioService.loopDidComplete
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                self?.loopsCompletedThisSession += 1
            }
            .store(in: &cancellables)

        // Record the session once playback finishes all loops.
        audioService.$state
            .dropFirst()
            .receive(on: RunLoop.main)
            .sink { [weak self] state in
                guard let self else { return }
                if state == .finished {
                    self.recordSessionIfPending()
                }
            }
            .store(in: &cancellables)
    }

    // MARK: - Lifecycle

    /// Prepares playback for a mantra (corpus or custom path).
    func prepare(mantra: Mantra, modelContext: ModelContext) {
        self.mantra = mantra
        self.modelContext = modelContext
        errorMessage = nil
        sessionStart = nil
        loopsCompletedThisSession = 0
        sessionRecorded = false

        do {
            if let url = try resolveAudioURL(for: mantra) {
                let duration = try audioService.load(url: url)
                timingPlan = resolveTimingPlan(mantra: mantra, duration: duration)
            } else {
                errorMessage = "No audio available for this mantra yet. Render the corpus first (see docs/architecture.md)."
                timingPlan = TimingPlan(entries: [])
            }
        } catch {
            errorMessage = error.localizedDescription
            timingPlan = TimingPlan(entries: [])
        }
    }

    // MARK: - Timing resolution

    private func resolveTimingPlan(mantra: Mantra, duration: Double) -> TimingPlan {
        // 1) Custom verses: backend-provided timings win.
        if let remote = mantra.remoteTiming, !remote.isEmpty {
            return TimingPlan(entries: Self.clamped(remote, to: duration))
        }
        // 2) Corpus: sidecar timing if bundled.
        if !mantra.isCustom,
           let sidecar = CorpusLoader().sidecarTiming(for: mantra.stableID) {
            return TimingPlan(entries: Self.clamped(sidecar, to: duration))
        }
        // 3) Fallback: on-device syllable-weight estimation.
        return TimingEstimator.estimate(padas: mantra.padas, duration: duration)
    }

    /// api-contract.md: "Clients must clamp: if `end > duration`, clamp to
    /// `duration`." Guards against backend/duration drift so the last word
    /// can't stay highlighted past the end of the audio.
    private static func clamped(_ entries: [TimingEntry], to duration: Double) -> [TimingEntry] {
        guard duration > 0 else { return entries }
        return entries.map { entry in
            let start = min(entry.start, duration)
            let end = min(max(entry.end, start), duration)
            return TimingEntry(
                text: entry.text,
                start: start,
                end: end,
                estimated: entry.estimated
            )
        }
    }

    private func resolveAudioURL(for mantra: Mantra) throws -> URL? {
        if mantra.isCustom, let cached = mantra.cachedAudio {
            return AudioCache.load(fileName: cached.fileName)
        }
        if let fileName = mantra.audioFileName {
            return CorpusLoader().audioURL(for: fileName)
        }
        return nil
    }

    // MARK: - Karaoke tracking

    private func updateActiveWord(position: Double) {
        guard let idx = timingPlan.index(at: position) else {
            if activeWordIndex != nil {
                activeWordIndex = nil
                activeWordText = ""
            }
            return
        }
        if activeWordIndex != idx {
            activeWordIndex = idx
            activeWordText = timingPlan.entries[idx].text
        }
    }

    // MARK: - Session recording

    private func recordSessionIfPending() {
        guard !sessionRecorded, loopsCompletedThisSession > 0,
              let mantra, let modelContext else { return }
        let repository = MantraRepository(modelContext: modelContext)
        try? repository.recordSession(
            mantra: mantra,
            startedAt: sessionStart ?? .now,
            loops: loopsCompletedThisSession
        )
        sessionRecorded = true
    }

    /// Call when the user leaves the chant screen mid-chant.
    func recordSessionOnExit() {
        recordSessionIfPending()
        audioService.stop()
    }
}
