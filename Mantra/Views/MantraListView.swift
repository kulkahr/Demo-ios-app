import SwiftData
import SwiftUI

/// Home screen: the mantra library with favorites and stats.
struct MantraListView: View {
    @Environment(\.modelContext) private var modelContext
    @Query(sort: \Mantra.createdAt) private var mantras: [Mantra]
    @EnvironmentObject private var settings: AppSettings
    @State private var searchText = ""
    @State private var showOnlyFavorites = false

    private var filtered: [Mantra] {
        mantras.filter { mantra in
            if showOnlyFavorites && !mantra.isFavorite { return false }
            if searchText.isEmpty { return true }
            return mantra.title.localizedCaseInsensitiveContains(searchText)
                || mantra.transliteration.localizedCaseInsensitiveContains(searchText)
        }
    }

    var body: some View {
        List {
            Section {
                ForEach(filtered) { mantra in
                    NavigationLink(value: mantra) {
                        MantraRow(mantra: mantra)
                    }
                    .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                        Button {
                            try? MantraRepository(modelContext: modelContext).toggleFavorite(mantra)
                        } label: {
                            Label(
                                mantra.isFavorite ? "Unfavorite" : "Favorite",
                                systemImage: mantra.isFavorite ? "star.slash" : "star.fill"
                            )
                        }
                        .tint(.yellow)
                    }
                }
                .onDelete(perform: delete)
            } header: {
                Text("Mantras")
            } footer: {
                if !settings.isEndpointConfigured {
                    Label(
                        "Custom chanting is disabled until a TTS endpoint is set in Settings.",
                        systemImage: "info.circle"
                    )
                }
            }
        }
        .searchable(text: $searchText, prompt: "Search mantras")
        .navigationTitle("Mantra")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button {
                    showOnlyFavorites.toggle()
                } label: {
                    Image(systemName: showOnlyFavorites ? "star.fill" : "star")
                }
            }
        }
        .navigationDestination(for: Mantra.self) { mantra in
            ChantView(mantra: mantra)
        }
    }

    private func delete(at offsets: IndexSet) {
        let repository = MantraRepository(modelContext: modelContext)
        for index in offsets {
            let mantra = filtered[index]
            guard mantra.isCustom else { continue } // corpus rows are seeded
            try? repository.deleteMantra(mantra)
        }
    }
}

private struct MantraRow: View {
    let mantra: Mantra

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(mantra.title)
                    .font(.headline)
                if mantra.isFavorite {
                    Image(systemName: "star.fill")
                        .font(.caption)
                        .foregroundStyle(.yellow)
                }
            }
            Text(mantra.transliteration.isEmpty ? mantra.padas.joined(separator: " ") : mantra.transliteration)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Text("\(MeterCatalog.displayName(for: mantra.meter)) · \(mantra.sourceName)")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 2)
    }
}
