import Foundation
import Testing
@testable import Mantra

/// Tests for the api-contract.md wire types and shard building.
struct VagdhenuContractTests {
    @Test func shardEntryShapeMatchesVagdhenuFormat() {
        let entry = VagdhenuClient.shardEntry(id: "x1", meter: "anushtubh", padas: ["ॐ", "स्वः"], seed: 7)
        #expect(entry["id"] as? String == "x1")
        #expect(entry["meter"] as? String == "anushtubh")
        #expect(entry["padas"] as? [String] == ["ॐ", "स्वः"])
        #expect(entry["seed"] as? Int == 7)
        // render.py requires no_sandhi on every shard entry.
        #expect(entry["no_sandhi"] as? Bool == true)
    }

    @Test func shardEntryOmitsSeedWhenNil() {
        let entry = VagdhenuClient.shardEntry(id: "x2", meter: "gayatri", padas: ["ॐ"])
        #expect(entry["seed"] == nil)
    }

    @Test func synthesizeRequestEncodesContractFields() throws {
        let request = SynthesisRequest(id: "id1", meter: "anushtubh", padas: ["ॐ", "भूर्भुवः"], seed: nil, text: "ॐ भूर्भुवः")
        let data = try JSONEncoder().encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        #expect(json["id"] as? String == "id1")
        #expect(json["meter"] as? String == "anushtubh")
        #expect((json["padas"] as? [String])?.count == 2)
        #expect(json["seed"] == nil) // omitted, not null
        #expect(json["text"] as? String == "ॐ भूर्भुवः")
    }

    @Test func synthesizeResponseDecodesContractShape() throws {
        let payload = """
        {
          "id": "custom-1",
          "audioBase64": "UklGRg==",
          "duration": 6.421,
          "timing": [
            {"text": "ॐ", "start": 0.0, "end": 0.92, "estimated": false},
            {"text": "स्वः", "start": 0.92, "end": 3.3, "estimated": false}
          ],
          "meter": "anushtubh",
          "cached": false
        }
        """.data(using: .utf8)!
        let response = try JSONDecoder().decode(SynthesisResponse.self, from: payload)
        #expect(response.id == "custom-1")
        #expect(response.duration == 6.421)
        #expect(response.timing.count == 2)
        #expect(response.timing[0].text == "ॐ")
        #expect(response.cached == false)
    }

    @Test func unconfiguredEndpointThrowsTypedError() async {
        let client = VagdhenuClient(endpoint: "")
        do {
            _ = try await client.synthesize(
                request: SynthesisRequest(id: "x", meter: "anushtubh", padas: ["ॐ"], seed: nil, text: "ॐ")
            )
            Issue.record("expected endpointNotConfigured")
        } catch let error as VagdhenuError {
            #expect(error == .endpointNotConfigured)
        } catch {
            Issue.record("unexpected error type: \(error)")
        }
    }
}
