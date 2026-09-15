import Foundation
import Testing
@testable import Mantra

/// Validates the seed corpus shape and cross-implementation parity.
struct CorpusTests {
    /// Repo-root path derived from this file's location — independent of the
    /// test runner's working directory.
    static let corpusURL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()          // Mantra/Tests
        .deletingLastPathComponent()          // Mantra
        .deletingLastPathComponent()          // repo root
        .appendingPathComponent("mantras/mantras.json")

    @Test func corpusDecodesAndIsValid() throws {
        let data = try Data(contentsOf: Self.corpusURL)
        let corpus = try JSONDecoder().decode(CorpusFile.self, from: data)
        #expect(corpus.schemaVersion == 1)
        #expect(!corpus.mantras.isEmpty)
        for mantra in corpus.mantras {
            #expect(!mantra.stableID.isEmpty)
            #expect(!mantra.padas.isEmpty)
            #expect(!mantra.meter.isEmpty)
            #expect(mantra.audioFileName.hasSuffix(".wav"))
        }
    }

    @Test func pythonAndSwiftEstimatorsAgree() {
        // Mirrors scripts/render_corpus.py akshara_count (n_aksharas port).
        // The contract is exact numeric parity per pada; see
        // TimingEstimatorTests for the full reference-value list.
        #expect(TimingEstimator.aksharaCount("गायत्रीमन्त्र") == 5)
        #expect(TimingEstimator.aksharaCount("गायत्रीमन्त्र") > TimingEstimator.aksharaCount("ॐ"))
    }
}
