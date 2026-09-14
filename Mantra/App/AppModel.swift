import Foundation
import SwiftData

/// App-wide startup model: seeds the mantra corpus into SwiftData once.
@MainActor
final class AppModel: ObservableObject {
    @Published var seedError: String?

    private var seedAttempted = false

    func seedIfNeeded(context: ModelContext) {
        guard !seedAttempted else { return }
        seedAttempted = true
        do {
            try MantraRepository(modelContext: context).seedIfNeeded()
        } catch {
            seedError = error.localizedDescription
        }
    }

    func dismissSeedError() {
        seedError = nil
    }
}
