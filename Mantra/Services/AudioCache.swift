import Foundation

/// In-memory representation of synthesized WAV bytes handed to the cache.
struct CachedAudioPayload {
    let data: Data
    let duration: TimeInterval
}

/// File-system cache for remotely synthesized WAVs, stored in
/// Application Support/audio-cache. The SwiftData `CachedAudio` model tracks
/// metadata; this enum owns the bytes on disk.
enum AudioCache {
    static var cacheDirectory: URL {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = support.appendingPathComponent("audio-cache", isDirectory: true)
        if !FileManager.default.fileExists(atPath: dir.path) {
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        return dir
    }

    static func url(for fileName: String) -> URL {
        cacheDirectory.appendingPathComponent(fileName)
    }

    static func store(payload: CachedAudioPayload, as fileName: String) throws {
        try payload.data.write(to: url(for: fileName), options: .atomic)
    }

    static func load(fileName: String) -> URL? {
        let fileURL = url(for: fileName)
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return nil }
        return fileURL
    }

    static func remove(fileName: String) {
        try? FileManager.default.removeItem(at: url(for: fileName))
    }
}
