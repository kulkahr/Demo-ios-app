import Combine
import Foundation
import SwiftUI

/// User settings backed by `UserDefaults`. The TTS endpoint is configuration,
/// never hard-coded (architecture.md §7).
@MainActor
final class AppSettings: ObservableObject {
    private enum Keys {
        static let ttsEndpoint = "ttsEndpoint"
        static let ttsAPIKey = "ttsApiKey"
        static let playbackSpeed = "playbackSpeed"
        static let loopCount = "loopCount"
    }

    @Published var ttsEndpoint: String {
        didSet { UserDefaults.standard.set(ttsEndpoint, forKey: Keys.ttsEndpoint) }
    }
    @Published var ttsAPIKey: String {
        didSet { UserDefaults.standard.set(ttsAPIKey, forKey: Keys.ttsAPIKey) }
    }
    @Published var playbackSpeed: Double {
        didSet { UserDefaults.standard.set(playbackSpeed, forKey: Keys.playbackSpeed) }
    }
    @Published var loopCount: Int {
        didSet { UserDefaults.standard.set(loopCount, forKey: Keys.loopCount) }
    }

    /// Modal URLs look like https://<workspace>--vagdhenu-tts-serve.modal.run
    var isEndpointConfigured: Bool {
        !ttsEndpoint.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    init() {
        let defaults = UserDefaults.standard
        ttsEndpoint = defaults.string(forKey: Keys.ttsEndpoint) ?? ""
        ttsAPIKey = defaults.string(forKey: Keys.ttsAPIKey) ?? ""
        let speed = defaults.double(forKey: Keys.playbackSpeed)
        playbackSpeed = speed > 0 ? speed : 1.0
        let loops = defaults.integer(forKey: Keys.loopCount)
        loopCount = loops > 0 ? loops : 1
    }

    func makeClient() -> VagdhenuClient {
        VagdhenuClient(endpoint: ttsEndpoint, apiKey: ttsAPIKey.isEmpty ? nil : ttsAPIKey)
    }
}
