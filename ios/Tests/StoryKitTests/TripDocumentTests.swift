import Foundation
import Testing

@testable import StoryKit

/// The committed fixture is a full pipeline run over `tests/fixtures/media/` (built with
/// `--no-cloud`, hence no landmarks or transcripts — their absent shapes are part of what is
/// being decoded).
struct TripDocumentTests {
    private func fixtureData() throws -> Data {
        let url = try #require(
            Bundle.module.url(
                forResource: "trip", withExtension: "json", subdirectory: "Fixtures"))
        return try Data(contentsOf: url)
    }

    @Test func decodesTheCommittedTrip() throws {
        let doc = try TripDocument.decode(from: fixtureData())
        #expect(doc.schemaVersion >= 1)
        #expect(doc.assets.count == 22)
        #expect(doc.days.count == 2)
        #expect(doc.trip.counts["media"] == 22)
        // context.supplied stays false until a context file is genuinely supplied (D2).
        #expect(doc.context.supplied == false)
    }

    @Test func assetKeysMatchTheirAssetIds() throws {
        let doc = try TripDocument.decode(from: fixtureData())
        for (key, asset) in doc.assets {
            #expect(key == asset.assetId)
            #expect(asset.contentHash.hasPrefix(asset.assetId))
        }
    }

    @Test func decodesVideoDetail() throws {
        let doc = try TripDocument.decode(from: fixtureData())
        let videos = doc.assets.values.filter { $0.kind == .video }
        #expect(videos.count == 3)
        for video in videos {
            let detail = try #require(video.video, "kind==video must carry the video object")
            // The --no-cloud fixture run transcribed nothing; whatever the status, a
            // non-transcribed clip must not carry transcript text.
            #expect(detail.transcriptStatus != .transcribed)
            #expect(detail.transcript == nil)
        }
        // The conditional runs the other way too: no image carries a video object.
        #expect(doc.assets.values.filter { $0.kind == .image }.allSatisfy { $0.video == nil })
    }

    // {id, kind, is_keeper} decoded fully is what makes the alternates strip (I42) possible.
    @Test func decodesClustersFully() throws {
        let doc = try TripDocument.decode(from: fixtureData())
        let clustered = doc.assets.values.compactMap(\.cluster)
        #expect(!clustered.isEmpty)
        let keepers = doc.assets.values.filter { $0.cluster?.isKeeper == true }
        #expect(!keepers.isEmpty)
        for asset in doc.assets.values {
            guard let cluster = asset.cluster, let id = cluster.id else { continue }
            // A cluster id is its keeper's asset_id, so it must resolve within the document.
            #expect(doc.assets[id] != nil, "cluster id \(id) resolves to no asset")
        }
    }

    @Test func decodesEventStructure() throws {
        let doc = try TripDocument.decode(from: fixtureData())
        for day in doc.days {
            for event in day.events {
                // An event id is <date>#<seq>, a function of the media set, never a rowid.
                #expect(event.id == "\(day.date)#\(event.seq)")
                for assetId in event.assets {
                    #expect(doc.assets[assetId] != nil)
                }
            }
        }
    }

    /// No unknown-field loss: every key present in the raw JSON with a non-null value must
    /// survive a decode → encode round trip. Null-valued keys are excused only because Codable
    /// omits absent optionals on encode; the decoder still maps every key the schema names.
    @Test func roundTripLosesNoPopulatedField() throws {
        let data = try fixtureData()
        let doc = try TripDocument.decode(from: data)
        let original = try JSONSerialization.jsonObject(with: data)
        let reencoded = try JSONSerialization.jsonObject(
            with: JSONEncoder().encode(doc))
        var missing: [String] = []
        collectMissingKeys(original: original, reencoded: reencoded, path: "$", into: &missing)
        #expect(missing.isEmpty, "keys lost in decode: \(missing.prefix(10))")
    }

    /// The schema half of the CI gate: `trip_schema.json` changing must break this test rather
    /// than surface in someone's hands. On failure: regenerate the fixture
    /// (`story-book build tests/fixtures/media --no-cloud`), update `TripDocument`, then this
    /// digest.
    @Test func schemaIsUnchangedSinceTheDecoderWasWritten() throws {
        let schema = repoRoot.appending(path: "src/story_book/trip_schema.json")
        #expect(
            try ContentHash.hex(of: schema)
                == "b2ecc457a3e41ae73d3075bb2bf24d2d4298cec8445f469dca5820cc2d905c2a"
                + "28beaf788c952c47ca1efc3e63673cc05f9dd7631035138e7c8ee58c834724f0")
    }

    private func collectMissingKeys(
        original: Any, reencoded: Any?, path: String, into missing: inout [String]
    ) {
        switch original {
        case let dict as [String: Any]:
            let reDict = reencoded as? [String: Any]
            for (key, value) in dict {
                if value is NSNull { continue }
                guard let reValue = reDict?[key] else {
                    missing.append("\(path).\(key)")
                    continue
                }
                collectMissingKeys(
                    original: value, reencoded: reValue, path: "\(path).\(key)", into: &missing)
            }
        case let array as [Any]:
            let reArray = reencoded as? [Any]
            for (i, value) in array.enumerated() {
                collectMissingKeys(
                    original: value,
                    reencoded: reArray.flatMap { i < $0.count ? $0[i] : nil },
                    path: "\(path)[\(i)]", into: &missing)
            }
        default:
            break
        }
    }
}
