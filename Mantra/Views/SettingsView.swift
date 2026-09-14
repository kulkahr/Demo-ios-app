import SwiftData
import SwiftUI

/// Settings: TTS endpoint/key configuration, playback defaults, stats.
struct SettingsView: View {
    @EnvironmentObject private var settings: AppSettings
    @Environment(\.modelContext) private var modelContext
    @State private var totalLoops = 0

    var body: some View {
        Form {
            Section {
                TextField("https://…--vagdhenu-tts-serve.modal.run", text: $settings.ttsEndpoint)
                    .textFieldStyle(.plain)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
                SecureField("API key (optional)", text: $settings.ttsAPIKey)
            } header: {
                Text("TTS endpoint (Modal)")
            } footer: {
                Text(
                    "Deploy the bundled worker with `modal deploy backend/modal_app.py` and paste the URL here. "
                    + "Requests go directly from this device to your endpoint; nothing is sent anywhere else."
                )
            }

            Section {
                LabeledContent("Speed", value: String(format: "%.1f×", settings.playbackSpeed))
                Slider(value: $settings.playbackSpeed, in: 0.5...2.0, step: 0.1)
                Stepper("Loops per chant: \(settings.loopCount)", value: $settings.loopCount, in: 1...108)
            } header: {
                Text("Playback")
            }

            Section("Stats") {
                LabeledContent("Total loops chanted", value: "\(totalLoops)")
            }
        }
        .navigationTitle("Settings")
        .onAppear { totalLoops = (try? MantraRepository(modelContext: modelContext).totalLoopsChanted()) ?? 0 }
    }
}
