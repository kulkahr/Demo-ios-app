import Combine
import Foundation
import SwiftData

/// Vagdhenu meter keys. The picker mirrors the meters the upstream reference
/// bank actually ships (ASCII wav-stem keys, verified against bank.json) —
/// render.py silently falls back to vasantatilakā for anything else, so
/// offering unknown keys would quietly render the wrong reference chant.
/// Notably `gayatri` is NOT in the bank: the Gāyatrī renders with the
/// vasantatilakā reference — the same choice the demo's Auto-detect makes.
enum MeterCatalog {
    static let defaultMeter = "anushtubh"
    static let all: [(String, String)] = [
        ("anushtubh", "Anuṣṭubh (śloka)"),
        ("vasantatilaka", "Vasantatilakā"),
        ("upajati", "Upajāti"),
        ("indravajra", "Indravajrā"),
        ("upendravajra", "Upendravajrā"),
        ("vamshastha", "Vaṃśastha"),
        ("rathoddhata", "Rathoddhatā"),
        ("shalini", "Śālinī"),
        ("indravamsha", "Indravaṃśā"),
        ("drutavilambita", "Drutavilambita"),
        ("bhujangaprayata", "Bhujaṅgaprayāta"),
        ("malini", "Mālinī"),
        ("shardulavikridita", "Śārdūlavikrīḍita"),
        ("sragdhara", "Sragdharā"),
        ("gadya", "Gadya (prose)"),
    ]

    /// Display names for meter keys that appear in corpus config but are not
    /// in the bank (shown in lists, never offered for synthesis).
    private static let extraDisplayNames: [String: String] = [
        "gayatri": "Gāyatrī",
    ]

    static func displayName(for key: String) -> String {
        all.first { $0.0 == key }?.1 ?? extraDisplayNames[key] ?? key
    }
}

/// Synthesizes a custom verse via the on-demand TTS path (Path B) and saves
/// the result as a custom `Mantra` with cached audio and backend timing.
@MainActor
final class CustomChantViewModel: ObservableObject {
    // MARK: - Input state (bound to the form)

    @Published var title: String = ""
    @Published var verseText: String = ""
    @Published var meter: String = MeterCatalog.defaultMeter

    // MARK: - Request state

    enum Phase: Equatable {
        case idle
        case synthesizing
        case done
        case failed(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var statusDetail: String = ""

    private var endpoint: String = ""
    private var apiKey: String = ""

    /// Applies user-configured endpoint settings (called from the view).
    func configure(endpoint: String, apiKey: String) {
        self.endpoint = endpoint
        self.apiKey = apiKey
    }

    /// Splits the verse text into padas on whitespace.
    func padas() -> [String] {
        verseText
            .split(whereSeparator: { $0.isWhitespace || $0 == "\n" })
            .map(String.init)
            .filter { !$0.isEmpty }
    }

    var canSynthesize: Bool {
        phase != .synthesizing && padas().count >= 2
    }

    /// Runs Path B: shard entry → worker → WAV + timing → saved custom mantra.
    func synthesizeAndSave(modelContext: ModelContext) async {
        let padas = padas()
        guard padas.count >= 2 else {
            phase = .failed("Enter at least two words (padas) to chant.")
            return
        }

        phase = .synthesizing
        statusDetail = "Contacting the chant engine…"

        let client = VagdhenuClient(endpoint: endpoint, apiKey: apiKey.isEmpty ? nil : apiKey)
        let request = SynthesisRequest(
            id: VagdhenuClient.makeID(),
            meter: meter,
            padas: padas,
            seed: nil,
            text: verseText.trimmingCharacters(in: .whitespacesAndNewlines)
        )

        do {
            statusDetail = "Synthesizing (the model may need to warm up)…"
            let response = try await client.synthesizeWithColdStartRetry(request: request)

            guard let wavData = Data(base64Encoded: response.audioBase64), !wavData.isEmpty else {
                phase = .failed("The service returned unusable audio data.")
                return
            }

            statusDetail = "Saving…"
            let repository = MantraRepository(modelContext: modelContext)
            let mantra = try repository.saveCustomMantra(
                title: title.trimmingCharacters(in: .whitespacesAndNewlines),
                transliteration: "",
                padas: padas,
                meter: response.meter,
                timing: response.timing,
                audio: CachedAudioPayload(data: wavData, duration: response.duration)
            )
            statusDetail = mantra.title
            phase = .done
        } catch let error as VagdhenuError {
            phase = .failed(error.localizedDescription)
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    /// Resets the form after a completed or failed synthesis.
    func reset() {
        phase = .idle
        statusDetail = ""
        title = ""
        verseText = ""
        meter = MeterCatalog.defaultMeter
    }
}
