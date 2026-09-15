import Foundation

/// Errors surfaced by the remote TTS path.
enum VagdhenuError: Error, LocalizedError, Equatable {
    case endpointNotConfigured
    case invalidResponse
    case httpStatus(code: Int, message: String)
    case rateLimited
    case serverColdStart
    case network(String)

    var errorDescription: String? {
        switch self {
        case .endpointNotConfigured:
            return "No TTS endpoint configured. Set it in Settings to synthesize custom chants."
        case .invalidResponse:
            return "The TTS service returned an unexpected response."
        case .httpStatus(let code, let message):
            return "TTS request failed (\(code)): \(message)"
        case .rateLimited:
            return "The TTS service is rate limiting requests. Try again shortly."
        case .serverColdStart:
            return "The TTS model is warming up."
        case .network(let detail):
            return "Network error: \(detail)"
        }
    }
}

/// Wire types from `docs/api-contract.md`.
struct SynthesisRequest: Encodable, Sendable {
    let id: String
    let meter: String
    let padas: [String]
    let seed: Int?
    let text: String
}

struct SynthesisResponse: Decodable, Sendable {
    let id: String
    let audioBase64: String
    let duration: Double
    let timing: [TimingEntry]
    let meter: String
    let cached: Bool
}

/// Builds shard entries and talks to the Modal-hosted Vagdhenu worker.
/// Endpoint and API key come from user settings (AppStorage), never hard-coded.
struct VagdhenuClient {
    var endpoint: String
    var apiKey: String?

    init(endpoint: String, apiKey: String? = nil) {
        self.endpoint = endpoint
        self.apiKey = apiKey
    }

    static func makeID() -> String {
        "custom-\(Int(Date().timeIntervalSince1970 * 1000))-\(Int.random(in: 100...999))"
    }

    /// Builds a shard entry per Vagdhenu's batch format.
    static func shardEntry(id: String, meter: String, padas: [String], seed: Int? = nil) -> [String: Any] {
        var entry: [String: Any] = [
            "id": id,
            "meter": meter,
            "padas": padas,
            // Required by render.py (indexed per clip; missing key fails every
            // clip with KeyError). true = padas are already traditionally
            // word-split; skip the renderer's automatic sandhi re-splitting.
            "no_sandhi": true,
        ]
        if let seed { entry["seed"] = seed }
        return entry
    }

    /// Synthesizes a chant and returns decoded WAV bytes plus timing.
    func synthesize(request: SynthesisRequest) async throws -> SynthesisResponse {
        guard !endpoint.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw VagdhenuError.endpointNotConfigured
        }
        guard var components = URLComponents(string: endpoint) else {
            throw VagdhenuError.network("invalid endpoint URL")
        }
        if components.path.isEmpty { components.path = "/synthesize" }
        guard let url = components.url else {
            throw VagdhenuError.network("invalid endpoint URL")
        }

        var urlRequest = URLRequest(url: url)
        urlRequest.httpMethod = "POST"
        urlRequest.timeoutInterval = 180 // GPU cold starts can be slow
        urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let apiKey, !apiKey.isEmpty {
            urlRequest.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        }
        urlRequest.httpBody = try JSONEncoder().encode(request)

        do {
            let (data, response) = try await URLSession.shared.data(for: urlRequest)
            guard let http = response as? HTTPURLResponse else {
                throw VagdhenuError.invalidResponse
            }
            try Self.validate(status: http.statusCode, data: data)
            return try JSONDecoder().decode(SynthesisResponse.self, from: data)
        } catch let error as VagdhenuError {
            throw error
        } catch is DecodingError {
            throw VagdhenuError.invalidResponse
        } catch {
            throw VagdhenuError.network(error.localizedDescription)
        }
    }

    /// Auto-retries 503 cold starts per api-contract.md (up to 2× after 5 s).
    func synthesizeWithColdStartRetry(request: SynthesisRequest) async throws -> SynthesisResponse {
        for attempt in 0...2 {
            do {
                return try await synthesize(request: request)
            } catch VagdhenuError.serverColdStart where attempt < 2 {
                try await Task.sleep(for: .seconds(5))
            }
        }
        // Unreachable: loop returns or throws; kept for exhaustiveness.
        fatalError("retry loop exited unexpectedly")
    }

    /// Maps HTTP status codes to typed errors per api-contract.md.
    private static func validate(status: Int, data: Data) throws {
        switch status {
        case 200...299:
            return
        case 400:
            throw VagdhenuError.httpStatus(code: 400, message: Self.errorMessage(from: data) ?? "Invalid request")
        case 401:
            throw VagdhenuError.httpStatus(code: 401, message: "Invalid API key")
        case 429:
            throw VagdhenuError.rateLimited
        case 503:
            throw VagdhenuError.serverColdStart
        default:
            throw VagdhenuError.httpStatus(code: status, message: Self.errorMessage(from: data) ?? "Request failed")
        }
    }

    private static func errorMessage(from data: Data) -> String? {
        struct Body: Decodable {
            struct E: Decodable { let message: String }
            let error: E
        }
        return (try? JSONDecoder().decode(Body.self, from: data))?.error.message
    }
}
