import SwiftUI

/// Karaoke chant player: Devanagari padas with word-level highlighting,
/// transport controls, loop counter, and meaning.
struct ChantView: View {
    let mantra: Mantra

    @Environment(\.modelContext) private var modelContext
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject private var settings: AppSettings
    @StateObject private var viewModel: ChantViewModel

    init(mantra: Mantra) {
        self.mantra = mantra
        _viewModel = StateObject(wrappedValue: ChantViewModel())
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 24) {
                if let error = viewModel.errorMessage {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .padding()
                        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
                }

                KaraokeText(
                    entries: viewModel.timingPlan.entries,
                    activeIndex: viewModel.activeWordIndex
                )

                Text(mantra.meaning)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal)

                LoopIndicator(current: audio.loopIndex, total: audio.totalLoops)
            }
            .padding()
        }
        .navigationTitle(mantra.title)
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) {
            TransportBar(
                isPlaying: audio.isPlaying,
                currentTime: audio.currentTime,
                duration: audio.duration,
                loopIndex: audio.loopIndex,
                totalLoops: audio.totalLoops,
                onPlayPause: togglePlay,
                onStop: { audio.stop() }
            )
            .padding()
            .background(.bar)
        }
        .onAppear {
            audio.totalLoops = settings.loopCount
            audio.setSpeed(Float(settings.playbackSpeed))
            viewModel.prepare(mantra: mantra, modelContext: modelContext)
        }
        .onDisappear {
            viewModel.recordSessionOnExit()
        }
    }

    private var audio: AudioPlayerService { viewModel.audioService }

    private func togglePlay() {
        if audio.isPlaying {
            audio.pause()
        } else {
            audio.play()
        }
    }
}

/// Devanagari karaoke: the active pada is highlighted while sounding.
struct KaraokeText: View {
    let entries: [TimingEntry]
    let activeIndex: Int?

    var body: some View {
        FlowLayout(spacing: 10) {
            ForEach(entries.indices, id: \.self) { index in
                Text(entries[index].text)
                    .font(.system(size: 28, weight: .semibold, design: .serif))
                    .foregroundStyle(index == activeIndex ? Color.orange : Color.primary)
                    .scaleEffect(index == activeIndex ? 1.08 : 1.0)
                    .animation(.easeInOut(duration: 0.18), value: activeIndex)
            }
        }
    }
}

/// Simple wrap-flow layout for pada chips.
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > width, x > 0 {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: width == .infinity ? x : width, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY
        var rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > bounds.maxX, x > bounds.minX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}

/// Dots showing chant loops.
struct LoopIndicator: View {
    let current: Int
    let total: Int

    var body: some View {
        HStack(spacing: 8) {
            ForEach(1...max(total, 1), id: \.self) { index in
                Circle()
                    .fill(index <= current ? Color.orange : Color.secondary.opacity(0.3))
                    .frame(width: 10, height: 10)
            }
        }
    }
}

/// Bottom transport bar.
struct TransportBar: View {
    let isPlaying: Bool
    let currentTime: Double
    let duration: Double
    let loopIndex: Int
    let totalLoops: Int
    let onPlayPause: () -> Void
    let onStop: () -> Void

    var body: some View {
        VStack(spacing: 8) {
            ProgressView(value: duration > 0 ? currentTime / duration : 0)
                .tint(.orange)
            HStack(spacing: 28) {
                Button(action: onStop) {
                    Image(systemName: "stop.fill")
                        .font(.title2)
                }
                Button(action: onPlayPause) {
                    Image(systemName: isPlaying ? "pause.circle.fill" : "play.circle.fill")
                        .font(.system(size: 56))
                }
                Label("\(loopIndex)/\(max(totalLoops, 1))", systemImage: "repeat")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
