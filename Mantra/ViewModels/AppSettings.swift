import Combine
import Foundation
import SwiftUI

/// User settings. The TTS endpoint is configuration (UserDefaults); the API
/// key is a credential and lives in the Keychain, never hard-coded and never
/// in plain preferences (architecture.md §7).
@MainActor
final class AppSettings: ObservableObject {
    private enum Keys {
        static let ttsEndpoint = "ttsEndpoint"
        static let ttsAPIKeyLegacy = "ttsApiKey" // pre-Keychain storage location
        static let playbackSpeed = "playbackSpeed"
        static let loopCount = "loopCount"
        static let keychainKey = "ttsAPIKey"
    }

    @Published var ttsEndpoint: String {
        didSet { UserDefaults.standard.set(ttsEndpoint, forKey: Keys.ttsEndpoint) }
    }

    /// API key backed by the Keychain. Written to the keychain on every change;
    /// reads come from the keychain so the value survives reinstalls of the
    /// same developer team only through iCloud Keychain sync, by design.
    @Published var ttsAPIKey: String {
        didSet { KeychainStore.set(ttsAPIKey.isEmpty ? nil : ttsAPIKey, forKey: Keys.keychainKey) }
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

        // One-time migration: a key stored in UserDefaults (pre-Keychain
        // builds) moves to the Keychain and is removed from plaintext.
        if let legacy = defaults.string(forKey: Keys.ttsAPIKeyLegacy), !legacy.isEmpty {
            KeychainStore.set(legacy, forKey: Keys.keychainKey)
            defaults.removeObject(forKey: Keys.ttsAPIKeyLegacy)
        }

        ttsEndpoint = defaults.string(forKey: Keys.ttsEndpoint) ?? ""
        ttsAPIKey = KeychainStore.get(Keys.keychainKey) ?? ""

        let speed = defaults.double(forKey: Keys.playbackSpeed)
        playbackSpeed = speed > 0 ? speed : 1.0
        let loops = defaults.integer(forKey: Keys.loopCount)
        loopCount = loops > 0 ? loops : 1
    }

    func makeClient() -> VagdhenuClient {
        VagdhenuClient(endpoint: ttsEndpoint, apiKey: ttsAPIKey.isEmpty ? nil : ttsAPIKey)
    }
}
