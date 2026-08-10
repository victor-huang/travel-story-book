import Foundation

/// Per-asset JSON uploaded beside the media at export time.
///
/// `PHAsset` hands over an absolute timestamp and coordinates directly, so the server can
/// prefer them over EXIF inference — but the sidecar must record which source each value came
/// from. Timezone resolution is the highest-risk logic in this project and stays on the
/// pipeline side: the sidecar carries a UTC instant and coordinates, both measured, and never
/// claims an offset it does not have.
///
/// House rule for the wire shape: absent data is `null` with a `source` beside it, never an
/// omitted key — a sidecar with no location must not read like a sidecar nobody wrote.
public struct Sidecar: Codable, Equatable, Sendable {
    public var schemaVersion: Int
    /// BLAKE2b of the exported bytes — the same digest the ledger records.
    public var contentHash: String
    /// `PHAssetResource.originalFilename`, preserved so `overrides.toml` corrections carry
    /// across from the laptop workflow.
    public var originalFilename: String
    public var creation: Creation
    public var location: Location

    public init(
        schemaVersion: Int = 1, contentHash: String, originalFilename: String,
        creation: Creation, location: Location
    ) {
        self.schemaVersion = schemaVersion
        self.contentHash = contentHash
        self.originalFilename = originalFilename
        self.creation = creation
        self.location = location
    }

    /// Where a value can come from. `photokit_asset` is a property of the asset in the
    /// library — a measurement, not a derivation. `unavailable` means the library holds
    /// nothing; the pipeline then falls back to what the file itself carries.
    public enum Source: String, Codable, Equatable, Sendable {
        case photokitAsset = "photokit_asset"
        case unavailable
    }

    public struct Creation: Codable, Equatable, Sendable {
        /// ISO 8601 UTC instant, e.g. `2025-07-17T12:34:56Z`. Absolute; carries no local zone.
        public var utc: String?
        public var source: Source

        public init(utc: String?, source: Source) {
            self.utc = utc
            self.source = source
        }

        public static let unavailable = Creation(utc: nil, source: .unavailable)

        public func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            // encode, not encodeIfPresent: null, never an omitted key.
            try container.encode(utc, forKey: .utc)
            try container.encode(source, forKey: .source)
        }
    }

    public struct Location: Codable, Equatable, Sendable {
        public var lat: Double?
        public var lon: Double?
        public var source: Source

        public init(lat: Double?, lon: Double?, source: Source) {
            self.lat = lat
            self.lon = lon
            self.source = source
        }

        public static let unavailable = Location(lat: nil, lon: nil, source: .unavailable)

        public func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            try container.encode(lat, forKey: .lat)
            try container.encode(lon, forKey: .lon)
            try container.encode(source, forKey: .source)
        }
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case contentHash = "content_hash"
        case originalFilename = "original_filename"
        case creation, location
    }

    /// The sidecar's name beside its media file: `IMG_1815.mov` → `IMG_1815.mov.sidecar.json`.
    /// Appending (rather than replacing the extension) keeps `IMG_1815.mov` and `IMG_1815.jpg`
    /// from colliding on one sidecar.
    public static func fileName(forMediaFilename filename: String) -> String {
        filename + ".sidecar.json"
    }

    public func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return try encoder.encode(self)
    }

    public static func decode(from data: Data) throws -> Sidecar {
        try JSONDecoder().decode(Sidecar.self, from: data)
    }
}
