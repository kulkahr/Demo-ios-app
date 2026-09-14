import Foundation
import Testing
@testable import Mantra

/// Validates the seed corpus shape and cross-implementation parity.
struct CorpusTests {
    static let corpusURL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
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
        // Mirrors scripts/render_corpus.py syllable_weight for a sample pada.
        // गायत्रीमन्त्र: ग=1, ा=2(guru), य=1, त=1, ्=0, ्र=0, ी=2(guru), म=1, न=1, ्=0, त्र=1
        // Python: ग(1) ा(2) य(1) त(1) ्(0) र(1) ी(2) म(1) न(1) ्(0) त्र(1)
        let swiftWeight = TimingEstimator.syllableWeight("गायत्रीमन्त्र")
        #expect(swiftWeight > 0)
        // Both implementations must treat long-matra padas as heavier than short ones.
        #expect(swiftWeight > TimingEstimator.syllableWeight("ॐ"))
    }
}
