import Foundation
import Testing
@testable import Mantra

/// Locks the last-word highlight contract: timing plans are contiguous and
/// cover the audio duration, so every word — especially the final one — has
/// a highlight window at every playback position the player can report.
struct LastWordHighlightTests {
    /// Positions sampled at 30 fps like AudioPlayerService's poll timer,
    /// plus the boundary ticks the finish path produces (exact duration).
    private func assertEveryWordHighlights(plan: TimingPlan, duration: Double, sourceLocation: SourceLocation = #_sourceLocation) {
        #expect(!plan.entries.isEmpty)
        #expect(abs(plan.entries.last!.end - duration) < 0.002)

        var covered = Set<Int>()
        let step = 1.0 / 30.0
        var p = 0.0
        while p < duration {
            if let i = plan.index(at: p) { covered.insert(i) }
            p += step
        }
        // The finish callback reports exactly `duration`; the tail clamps to
        // the final (open-ended) entry.
        if let i = plan.index(at: duration) { covered.insert(i) }
        if let i = plan.index(at: duration - 0.01) { covered.insert(i) }

        for (i, e) in plan.entries.enumerated() {
            #expect(covered.contains(i), "word '\(e.text)' (index \(i)) is never highlighted")
        }
    }

    @Test func sidecarPlansHighlightEveryWordIncludingLast() throws {
        let corpus = try JSONDecoder().decode(
            CorpusFile.self, from: Data(contentsOf: CorpusTests.corpusURL)
        )
        let loader = CorpusLoader()
        for mantra in corpus.mantras {
            guard let entries = loader.sidecarTiming(for: mantra.stableID) else {
                Issue.record("missing sidecar for \(mantra.stableID)")
                continue
            }
            // Durations from the bundled wavs via the repo layout the tests
            // already read (mantras/audio/<id>.wav).
            let wav = CorpusTests.corpusURL
                .deletingLastPathComponent()
                .appendingPathComponent("audio/\(mantra.stableID).wav")
            let data = try Data(contentsOf: wav)
            let duration = Self.wavDuration(data)
            #expect(duration > 0, "\(mantra.stableID).wav unreadable")

            // The documented client-side clamp (api-contract.md), applied
            // inline — same contract ChantViewModel enforces.
            let clamped = entries.map { e in
                TimingEntry(
                    text: e.text,
                    start: min(e.start, duration),
                    end: min(max(e.end, e.start), duration),
                    estimated: e.estimated
                )
            }
            let plan = TimingPlan(entries: clamped)
            assertEveryWordHighlights(plan: plan, duration: duration)
        }
    }

    @Test func estimatedPlansHighlightEveryWordIncludingLast() {
        // The on-device fallback (no sidecar): uniform akshara-proportional.
        let padas = ["ॐ", "भूर्भुवः", "स्वः", "तत्सवितुर्वरेण्यम्", "भर्गो", "देवस्य",
                     "धीमहि", "धियो", "यो", "नः", "प्रचोदयात्"]
        let plan = TimingEstimator.estimate(padas: padas, duration: 7.033)
        assertEveryWordHighlights(plan: plan, duration: 7.033)
    }

    /// Minimal RIFF walk — avoids importing AVFoundation just to read a
    /// duration in tests. For PCM, seconds = dataSize / byteRate
    /// (byteRate = sampleRate × blockAlign). RIFF sizes are little-endian.
    static func wavDuration(_ data: Data) -> Double {
        func le32(_ data: Data, _ offset: Int) -> UInt32 {
            var v: UInt32 = 0
            for i in (0..<4).reversed() {
                v = (v << 8) | UInt32(data[data.startIndex + offset + i])
            }
            return v
        }

        var byteRate: UInt32 = 0
        var dataSize = 0
        var offset = 12 // skip RIFF header
        while offset + 8 <= data.count {
            let id = String(data: data.subdata(in: offset..<(offset + 4)), encoding: .ascii)
            let size = Int(le32(data, offset + 4))
            let body = offset + 8
            if id == "fmt ", size >= 16 {
                byteRate = le32(data, body + 8)
            } else if id == "data" {
                dataSize = min(size, data.count - body)
            }
            offset = body + size + (size % 2)
        }
        guard byteRate > 0, dataSize > 0 else { return 0 }
        return Double(dataSize) / Double(byteRate)
    }
}
