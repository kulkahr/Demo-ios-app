import Foundation
import Testing
@testable import Mantra

/// SDLC Phase 5: Swift Testing suite for the timing engine.
struct TimingEstimatorTests {
    @Test func weightsCountGuruAndLaghu() {
        // "ॐ" counts as one mora (no vowel marks).
        #expect(TimingEstimator.syllableWeight("ॐ") == 1)
        // Long ā doubles the weight: भू (2) vs भ (1).
        #expect(TimingEstimator.syllableWeight("भूर्भुवः") > TimingEstimator.syllableWeight("स्वः"))
        // Empty/cluster-only strings still weigh at least 1.
        #expect(TimingEstimator.syllableWeight("्") == 1)
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
        // Estimated flag is always true from the estimator.
        #expect(plan.entries.allSatisfy(\.estimated))
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
