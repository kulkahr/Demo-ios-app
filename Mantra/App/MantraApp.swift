import SwiftData
import SwiftUI

@main
struct MantraApp: App {
    var body: some Scene {
        WindowGroup {
            RootView()
        }
        .modelContainer(for: [Mantra.self, ChantSession.self, CachedAudio.self])
    }
}
