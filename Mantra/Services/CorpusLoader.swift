import Foundation

/// Decoded shape of `mantras/mantras.json`.
struct CorpusFile: Codable {
    let schemaVersion: Int
    let mantras: [CorpusMantra]

    struct CorpusMantra: Codable {
        let stableID: String
        let title: String
        let transliteration: String
        let padas: [String]
        let meter: String
        let meaning: String
        let sourceName: String
        let audioFileName: String
    }
}

enum CorpusError: Error, LocalizedError {
    case missingCorpus
    case invalidCorpus(String)

    var errorDescription: String? {
        switch self {
        case .missingCorpus:
            return "mantras.json was not found in the app bundle."
        case .invalidCorpus(let detail):
            return "mantras.json is invalid: \(detail)"
        }
    }
}

/// Resolves corpus resources (JSON, WAV audio, timing sidecars) from the app
/// bundle, tolerating both folder-reference (`Mantras/...`) and flattened
/// resource layouts.
struct CorpusLoader {
    /// Loads and decodes the seed corpus.
    func loadCorpus() throws -> CorpusFile {
        guard let url = locate("mantras.json", subdirectory: "Mantras") else {
            throw CorpusError.missingCorpus
        }
        do {
            return try JSONDecoder().decode(CorpusFile.self, from: Data(contentsOf: url))
        } catch {
            throw CorpusError.invalidCorpus(String(describing: error))
        }
    }

    /// Bundle URL for a corpus mantra's rendered WAV, if present.
    func audioURL(for fileName: String) -> URL? {
        locate(fileName, subdirectory: "Mantras/audio")
    }

    /// Sidecar timing JSON contents for a mantra, if present.
    func sidecarTiming(for stableID: String) -> [TimingEntry]? {
        guard let url = locate("\(stableID).json", subdirectory: "Mantras/timing"),
              let data = try? Data(contentsOf: url)
        else { return nil }
        return try? JSONDecoder().decode([TimingEntry].self, from: data)
    }

    // MARK: - Resource resolution

    private func locate(_ resource: String, subdirectory: String) -> URL? {
        let name = (resource as NSString).deletingPathExtension
        let ext = (resource as NSString).pathExtension

        // Folder-reference layout: bundle/Mantras/<sub>/file
        let dirs = subdirectory.isEmpty ? ["Mantras"] : ["Mantras/\(subdirectory)", "Mantras"]
        for dir in dirs {
            if let url = Bundle.main.url(forResource: name, withExtension: ext, subdirectory: dir) {
                return url
            }
        }
        // Flattened layout: bundle/file
        return Bundle.main.url(forResource: name, withExtension: ext)
    }
}
