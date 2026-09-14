import SwiftData
import SwiftUI

/// Custom chant form: enter Devanagari text + meter, synthesize via the
/// remote TTS endpoint, and save the result as a custom mantra.
struct CustomChantView: View {
    @Environment(\.modelContext) private var modelContext
    @EnvironmentObject private var settings: AppSettings
    @StateObject private var viewModel = CustomChantViewModel()

    var body: some View {
        Form {
            Section {
                TextField("Title (optional)", text: $viewModel.title)
                TextField("Verse (Devanagari, space-separated padas)", text: $viewModel.verseText, axis: .vertical)
                    .lineLimit(3...6)
                    .font(.system(size: 20, design: .serif))
                Picker("Meter", selection: $viewModel.meter) {
                    ForEach(MeterCatalog.all, id: \.0) { key, name in
                        Text(name).tag(key)
                    }
                }
            } header: {
                Text("Verse")
            } footer: {
                Text("Words are separated by spaces. Each word becomes a karaoke step.")
            }

            Section {
                Button {
                    Task { await viewModel.synthesizeAndSave(modelContext: modelContext) }
                } label: {
                    HStack {
                        Image(systemName: "waveform.badge.magnifyingglass")
                        Text("Synthesize")
                    }
                }
                .disabled(!viewModel.canSynthesize || !settings.isEndpointConfigured)

                if case .synthesizing = viewModel.phase {
                    HStack {
                        ProgressView()
                        Text(viewModel.statusDetail)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
            } header: {
                Text("Render")
            } footer: {
                if !settings.isEndpointConfigured {
                    Label(
                        "Set your Modal TTS endpoint in Settings to enable synthesis.",
                        systemImage: "info.circle"
                    )
                }
            }

            if case .failed(let message) = viewModel.phase {
                Section {
                    Label(message, systemImage: "xmark.octagon")
                        .foregroundStyle(.red)
                }
            }

            if case .done = viewModel.phase {
                Section {
                    Label("Saved to your library. Find it under Custom.", systemImage: "checkmark.circle")
                        .foregroundStyle(.green)
                    Button("Chant another") { viewModel.reset() }
                }
            }
        }
        .navigationTitle("Custom Chant")
        .onAppear {
            viewModel.configure(endpoint: settings.ttsEndpoint, apiKey: settings.ttsAPIKey)
        }
    }
}
