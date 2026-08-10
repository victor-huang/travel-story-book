import Foundation
import Testing

@testable import StoryKit

struct SidecarTests {
    private let populated = Sidecar(
        contentHash: String(repeating: "ab", count: 64),
        originalFilename: "IMG_1815.mov",
        creation: .init(utc: "2025-07-17T12:34:56Z", source: .photokitAsset),
        location: .init(lat: 46.558, lon: 8.561, source: .photokitAsset))

    @Test func roundTripsThroughEncodeDecode() throws {
        let decoded = try Sidecar.decode(from: populated.encoded())
        #expect(decoded == populated)
    }

    @Test func roundTripsUnavailableValues() throws {
        let bare = Sidecar(
            contentHash: String(repeating: "cd", count: 64),
            originalFilename: "DSC00042.JPG",
            creation: .unavailable,
            location: .unavailable)
        let decoded = try Sidecar.decode(from: bare.encoded())
        #expect(decoded == bare)
        #expect(decoded.creation.utc == nil)
        #expect(decoded.creation.source == .unavailable)
    }

    // Absent data is null with a source beside it, never an omitted key — a sidecar with no
    // location must not read like a sidecar nobody wrote.
    @Test func absentValuesEncodeAsNullNotOmittedKeys() throws {
        let bare = Sidecar(
            contentHash: String(repeating: "cd", count: 64),
            originalFilename: "DSC00042.JPG",
            creation: .unavailable,
            location: .unavailable)
        let json = try #require(
            try JSONSerialization.jsonObject(with: bare.encoded()) as? [String: Any])
        let creation = try #require(json["creation"] as? [String: Any])
        let location = try #require(json["location"] as? [String: Any])
        #expect(creation["utc"] is NSNull)
        #expect(location["lat"] is NSNull)
        #expect(location["lon"] is NSNull)
        #expect(creation["source"] as? String == "unavailable")
    }

    @Test func wireKeysAreSnakeCase() throws {
        let json = try #require(
            try JSONSerialization.jsonObject(with: populated.encoded()) as? [String: Any])
        #expect(
            Set(json.keys)
                == ["schema_version", "content_hash", "original_filename", "creation", "location"])
    }

    // IMG_1815.mov and IMG_1815.jpg must not collide on one sidecar name.
    @Test func sidecarNameAppendsRatherThanReplacesTheExtension() {
        #expect(
            Sidecar.fileName(forMediaFilename: "IMG_1815.mov") == "IMG_1815.mov.sidecar.json")
        #expect(
            Sidecar.fileName(forMediaFilename: "IMG_1815.jpg") != Sidecar.fileName(
                forMediaFilename: "IMG_1815.mov"))
    }
}
