import Foundation
import Testing
@testable import Mantra

/// SDLC Phase 5: Swift Testing suite for the timing engine.
/// The structural model mirrors Vagdhenu's synthesis (per-pada speech =
/// aksharas × meter sec-per-syllable + fixed inter-pada gaps, rescaled to the
/// audio). Expected values are cross-checked against the Python
/// implementations in `scripts/render_corpus.py`, `scripts/kaggle_render.py`
/// and `backend/modal_app.py` (scalar-level parity).
struct TimingEstimatorTests {
    @Test func aksharasMatchVagdhenuNAksharas() {
        // Independent vowel, non-halant consonant, halant cluster, ॐ.
        #expect(TimingEstimator.aksharaCount("ॐ") == 1)
        #expect(TimingEstimator.aksharaCount("आ") == 1)
        #expect(TimingEstimator.aksharaCount("क") == 1)
        // क् starts a cluster (0) + ष (1) + कं (1) = 2 aksharas.
        #expect(TimingEstimator.aksharaCount("क्षकं") == 2)
        // Matras, anusvāra, visarga add nothing, and a halant consonant
        // joins the next syllable: भूर्भुवः = भ + र्(joins) + भ + व = 3.
        #expect(TimingEstimator.aksharaCount("भूर्भुवः") == 3)
        // "स्वः" = स् (0) + व (1) = 1.
        #expect(TimingEstimator.aksharaCount("स्वः") == 1)
        // Virama-only strings still count at least 1.
        #expect(TimingEstimator.aksharaCount("्") == 1)
        // Reference values verified against render_corpus.py akshara_count.
        #expect(TimingEstimator.aksharaCount("तत्सवितुर्वरेण्यम्") == 7)
        #expect(TimingEstimator.aksharaCount("प्रचोदयात्") == 4)
        #expect(TimingEstimator.aksharaCount("नावधीतमस्तु") == 6)
    }

    @Test func meterSelectsReferenceSpeechRate() {
        #expect(TimingEstimator.secPerSyllable(for: "anushtubh") == 0.326)
        #expect(TimingEstimator.secPerSyllable(for: "anuṣṭubh") == 0.326)
        // Unknown meter (e.g. "gayatri") falls back to vasantatilakā's rate,
        // matching render.py's FALLBACK_METER.
        #expect(TimingEstimator.secPerSyllable(for: "gayatri")
                == TimingEstimator.defaultSecPerSyllable)
        #expect(TimingEstimator.secPerSyllable(for: nil)
                == TimingEstimator.defaultSecPerSyllable)
    }

    @Test func estimateCoversFullDurationInOrder() {
        let padas = ["ॐ", "भूर्भुवः", "स्वः"]
        let plan = TimingEstimator.estimate(padas: padas, duration: 10)
        #expect(plan.entries.count == 3)
        #expect(plan.entries[0].start == 0)
        #expect(plan.entries.last?.end == 10)
        // Monotonic, non-overlapping, gap-free: every entry starts exactly
        // where the previous one ends.
        for pair in zip(plan.entries, plan.entries.dropFirst()) {
            #expect(pair.0.end == pair.1.start)
        }
        #expect(plan.entries.allSatisfy { $0.estimated })
    }

    @Test func shortWordsAreNeverSkipped() {
        // Every pada occupies a non-zero span that starts at its speech
        // onset — this is the regression test for "skips short word but
        // highlights all": short words like ॐ previously got sub-frame spans
        // under proportional weighting.
        let padas = ["ॐ", "सह", "नौ", "मा", "यो", "नः"]
        let plan = TimingEstimator.estimate(padas: padas, duration: 12)
        for entry in plan.entries {
            #expect(entry.duration > 0.2)
            #expect(plan.index(at: entry.start + 0.01) != nil)
        }
        // The first onset is exactly 0 (chant starts with speech, not silence).
        #expect(plan.entries[0].start == 0)
    }

    @Test func longerWordsGetLongerSpans() {
        // भूर्भुवः (4 aksharas) vs ॐ (1 akshara): the speech portion of the
        // longer word must dominate its span.
        let plan = TimingEstimator.estimate(padas: ["भूर्भुवः", "ॐ"], duration: 9)
        let longSpan = plan.entries[0].duration
        let shortSpan = plan.entries[1].duration
        #expect(longSpan > shortSpan)
    }

    @Test func gayatriBoundariesMatchPythonReference() {
        // Golden values from scripts/render_corpus.py estimate_timing with
        // the real rendered durations (meter "gayatri" -> vasantatilaka rate;
        // 28 aksharas, gap_total 5.70, k = (11.753 − 5.70) / (28 × 0.259)
        // ≈ 0.8347).
        let padas = [
            "ॐ", "भूर्भुवः", "स्वः", "तत्सवितुर्वरेण्यम्", "भर्गो", "देवस्य",
            "धीमहि", "धियो", "यो", "नः", "प्रचोदयात्",
        ]
        let plan = TimingEstimator.estimate(padas: padas, duration: 11.753, meter: "gayatri")
        #expect(plan.entries.count == 11)
        #expect(abs(plan.entries[0].end - 0.766) < 0.01)          // ॐ speech
        #expect(abs(plan.entries[1].start - 0.766) < 0.01)        // भूर्भुवः onset
        #expect(abs(plan.entries[1].end - 1.965) < 0.01)
        #expect(abs(plan.entries[3].start - 2.731) < 0.01)        // तत्सवितुर्वरेण्यम्
        #expect(abs(plan.entries.last!.end - 11.753) < 0.001)
        // Short words keep usable spans.
        #expect(plan.entries.first!.duration > 0.4)
        #expect(plan.entries[8].duration > 0.4)                   // यो
    }

    @Test func emptyInputYieldsEmptyPlan() {
        #expect(TimingEstimator.estimate(padas: [], duration: 5).entries.isEmpty)
        #expect(TimingEstimator.estimate(padas: ["ॐ"], duration: 0).entries.isEmpty)
    }

    @Test func halantFinalPadasGetLongerGaps() {
        // प्रचोदयात् ends in virāma → +0.20s gap after it, mirroring
        // render.py's --gap_halant.
        let a = TimingEstimator.estimate(padas: ["भर्गो", "देवस्य"], duration: 6)
        let b = TimingEstimator.estimate(padas: ["प्रचोदयात्", "देवस्य"], duration: 6)
        // With the same duration, the halant gap pushes देवस्य's onset later.
        #expect(b.entries[1].start > a.entries[1].start)
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
