import Foundation
import Security

/// Minimal generic-password wrapper around the Keychain.
///
/// The TTS API key is a credential, so it lives in the Keychain — not
/// UserDefaults (architecture.md §7). Items are `ThisDeviceOnly` so they are
/// never restored to other devices through backups.
enum KeychainStore {
    /// Namespace for items stored by this app.
    private static let service = "com.mantra.tts"

    static func get(_ key: String) -> String? {
        var query = baseQuery(for: key)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    /// Stores the value, replacing any existing item. `nil` or empty deletes.
    static func set(_ value: String?, forKey key: String) {
        guard let value, !value.isEmpty else {
            delete(key)
            return
        }
        let data = Data(value.utf8)

        var query = baseQuery(for: key)
        let attributesToUpdate: [String: Any] = [kSecValueData as String: data]

        // Update in place when the item already exists.
        let updateStatus = SecItemUpdate(query as CFDictionary, attributesToUpdate as CFDictionary)
        if updateStatus == errSecItemNotFound {
            query[kSecValueData as String] = data
            query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            let addStatus = SecItemAdd(query as CFDictionary, nil)
            if addStatus != errSecSuccess {
                // Keychain failure must not crash the app; playback still
                // works, custom chants just won't authenticate.
            }
        }
    }

    static func delete(_ key: String) {
        SecItemDelete(baseQuery(for: key) as CFDictionary)
    }

    private static func baseQuery(for key: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
    }
}
