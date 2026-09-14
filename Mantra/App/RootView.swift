import SwiftUI

/// Root tab scaffold: Library, Custom chant, Settings.
struct RootView: View {
    @StateObject private var settings = AppSettings()
    @StateObject private var appModel = AppModel()
    @Environment(\.modelContext) private var modelContext

    var body: some View {
        TabView {
            NavigationStack {
                MantraListView()
            }
            .tabItem { Label("Library", systemImage: "text.book.closed.fill") }

            NavigationStack {
                CustomChantView()
            }
            .tabItem { Label("Custom", systemImage: "waveform") }

            NavigationStack {
                SettingsView()
            }
            .tabItem { Label("Settings", systemImage: "gearshape.fill") }
        }
        .tint(.orange)
        .environmentObject(settings)
        .task { appModel.seedIfNeeded(context: modelContext) }
        .alert(
            "Could not load the mantra corpus",
            isPresented: Binding(
                get: { appModel.seedError != nil },
                set: { _ in appModel.dismissSeedError() }
            )
        ) {
            Button("OK", role: .cancel) { appModel.dismissSeedError() }
        } message: {
            Text(appModel.seedError ?? "")
        }
    }
}
