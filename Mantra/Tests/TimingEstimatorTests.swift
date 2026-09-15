import Foundation
import Testing
@testable import Mantra

/// SDLC Phase 5: Swift Testing suite for the timing engine.
/// Expected weights are cross-checked against the Python implementations in
/// `scripts/render_corpus.py` and `backend/modal_app.py` (scalar-level parity).
struct TimingEstimatorTests {
    @Test func weightsCountGuruAndLaghu() {
        // "ॐ" counts as one mora (no vowel marks).
        #expect(TimingEstimator.syllableWeight("ॐ") == 1)
        // Virama-only strings still weigh at least 1.
        #expect(TimingEstimator.syllableWeight("्") == 1)
        // Dependent matra signs are detected per Unicode scalar:
        // क (1) vs का (1 + 2) vs सि (1 + 1) vs सी (1 + 2).
        #expect(TimingEstimator.syllableWeight("क") == 1)
        #expect(TimingEstimator.syllableWeight("का") == 3)
        #expect(TimingEstimator.syllableWeight("सि") == 2)
        #expect(TimingEstimator.syllableWeight("सी") == 3)
        // Visarga is guru: स्(1) + व(1) + ः(2).
        #expect(TimingEstimator.syllableWeight("स्वः") == 4)
    }

    @Test func weightsMatchPythonReference() {
        // Values computed by running scripts/render_corpus.py syllable_weight.
        #expect(TimingEstimator.syllableWeight("भूर्भुवः") == 9)
        #expect(TimingEstimator.syllableWeight("गायत्रीमन्त्र") == 12)
        #expect(TimingEstimator.syllableWeight("नावधीतमस्तु") == 12)
        #expect(TimingEstimator.syllableWeight("प्रचोदयात्") == 10)
    }

    @Test func estimateCoversFullDurationInOrder() {
        let padas = ["ॐ", "भूर्भुवः", "स्वः"]
        let plan = TimingEstimator.estimate(padas: padas, duration: 10)
        #expect(plan.entries.count == 3)
        #expect(plan.entries[0].start == 0)
        #expect(plan.entries.last?.end == 10)
        // Monotonic, non-overlapping.
        for pair in zip(plan.entries, plan.entries.dropFirst()) {
            #expect(pair.0.end == pair.1.start)
        }
        // Estimated flag is always true from the estimator. Explicit closure:
        // a key-path-as-function argument to the rethrows `allSatisfy` can be
        // typed as throwing inside #expect's macro expansion ("Call can throw,
        // but it is not marked with 'try'").
        #expect(plan.entries.allSatisfy { $0.estimated })
    }

    @Test func longerWordsGetLongerSpans() {
        let plan = TimingEstimator.estimate(padas: ["भूर्भुवः", "ॐ"], duration: 9)
        let longSpan = plan.entries[0].end - plan.entries[0].start
        let shortSpan = plan.entries[1].end - plan.entries[1].start
        #expect(longSpan > shortSpan)
    }

    @Test func emptyInputYieldsEmptyPlan() {
        #expect(TimingEstimator.estimate(padas: [], duration: 5).entries.isEmpty)
        #expect(TimingEstimator.estimate(padas: ["ॐ"], duration: 0).entries.isEmpty)
    }
}

struct TimingPlanTests {
    @Test func entryLookupAtPosition() {
        let plan = TimingPlan(entries: [
            TimingEntry(text: "a", start: 0, end: 1, estimated: false),
            TimingEntry(text: "b", start: 1, end: 2, estimated: false),
        ])
        #expect(plan.entry(at: 0.5)?.text == "a")
        #expect(plan.entry(at: 1.0)?.text == "b")
        #expect(plan.entry(at: 2.0) == nil) // past the end
        #expect(plan.index(at: 1.5) == 1)
    }
}
