import Foundation

/// A read-only view of `trip.json`, the published contract
/// (`src/story_book/trip_schema.json`). Everything the app displays comes from here.
///
/// The app computes nothing: no days, no events, no selection. A story is an overlay, never a
/// source of structure, and the same rule governs the app. Absent data is `null` with a status
/// field beside it, never an omitted key — so every optional here is a value the pipeline
/// explicitly declined to supply, not a hole in the decoder.
///
/// Enums (`kind`, `cluster.kind`, `subtype`, `transcript_status`, `orientation`) decode
/// strictly: a value outside the published vocabulary fails the decode rather than passing as
/// something it is not.
public struct TripDocument: Codable, Equatable, Sendable {
    public var schemaVersion: Int
    public var generator: Generator
    public var trip: Trip
    public var privacy: Privacy
    public var context: Context
    /// Keyed by `asset_id`, a prefix of the BLAKE2b content hash.
    public var assets: [String: Asset]
    public var days: [Day]
    public var tripHighlights: [String]

    // Explicit keys throughout, never `convertFromSnakeCase`: the strategy also rewrites
    // data-bearing dictionary keys, so counts["day_highlights"] would silently become
    // "dayHighlights".
    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case generator, trip, privacy, context, assets, days
        case tripHighlights = "trip_highlights"
    }

    public struct Generator: Codable, Equatable, Sendable {
        public var tool: String
        public var version: String
    }

    public struct Trip: Codable, Equatable, Sendable {
        public var name: String?
        public var startLocal: String?
        public var endLocal: String?
        public var counts: [String: Int]
        public var timezone: String?
        public var dayAssignmentRule: String
        public var startUtc: String?
        public var endUtc: String?

        private enum CodingKeys: String, CodingKey {
            case name
            case startLocal = "start_local"
            case endLocal = "end_local"
            case counts, timezone
            case dayAssignmentRule = "day_assignment_rule"
            case startUtc = "start_utc"
            case endUtc = "end_utc"
        }
    }

    public struct Privacy: Codable, Equatable, Sendable {
        public var homeConfigured: Bool
        public var exclusionKm: Double?
        public var excludedNearHome: Int
        public var excludedByOverride: Int
        public var excludedByContentClass: Int
        public var excludedContentClasses: [String]

        private enum CodingKeys: String, CodingKey {
            case homeConfigured = "home_configured"
            case exclusionKm = "exclusion_km"
            case excludedNearHome = "excluded_near_home"
            case excludedByOverride = "excluded_by_override"
            case excludedByContentClass = "excluded_by_content_class"
            case excludedContentClasses = "excluded_content_classes"
        }
    }

    public struct Context: Codable, Equatable, Sendable {
        /// Stays `false` until a context file is genuinely supplied (D2). Inferred context must
        /// never set it — an artifact never overstates its contents.
        public var supplied: Bool
        public var journalVoice: String?
        public var travelers: [Traveler]
        public var knownPlans: [String]
        public var notes: [String]

        private enum CodingKeys: String, CodingKey {
            case supplied
            case journalVoice = "journal_voice"
            case travelers
            case knownPlans = "known_plans"
            case notes
        }

        public struct Traveler: Codable, Equatable, Sendable {
            public var role: String
            public var name: String?
        }
    }

    public struct Asset: Codable, Equatable, Sendable {
        public var assetId: String
        public var contentHash: String
        public var filename: String
        public var kind: Kind
        public var bytes: Int
        /// Local wall time carrying its UTC offset when known. Order by UTC, split days by
        /// local — never mix this with a naive string.
        public var takenLocal: String?
        public var takenUtc: String?
        public var timezone: Timezone
        public var day: String?
        public var eventId: String?
        public var location: Location?
        public var nearHome: Bool
        public var quality: Quality?
        public var cluster: Cluster?
        public var selected: [String: Selection]
        public var landmarkIds: [Int]
        /// Present exactly when `kind == .video` (enforced by the schema's conditional).
        public var video: Video?
        public var thumbnail: String?
        public var preview: String?
        public var geometry: Geometry
        /// The date actually taken, which differs from `day` for anything shot after midnight.
        public var calendarDate: String?

        private enum CodingKeys: String, CodingKey {
            case assetId = "asset_id"
            case contentHash = "content_hash"
            case filename, kind, bytes
            case takenLocal = "taken_local"
            case takenUtc = "taken_utc"
            case timezone, day
            case eventId = "event_id"
            case location
            case nearHome = "near_home"
            case quality, cluster, selected
            case landmarkIds = "landmark_ids"
            case video, thumbnail, preview, geometry
            case calendarDate = "calendar_date"
        }

        public enum Kind: String, Codable, Equatable, Sendable {
            case image
            case video
        }
    }

    public struct Timezone: Codable, Equatable, Sendable {
        public var name: String?
        public var offsetMinutes: Int?
        public var source: String

        private enum CodingKeys: String, CodingKey {
            case name
            case offsetMinutes = "offset_minutes"
            case source
        }
    }

    public struct Location: Codable, Equatable, Sendable {
        public var lat: Double
        public var lon: Double
        public var source: String
        public var confidence: Double?
        public var place: Place?
    }

    public struct Place: Codable, Equatable, Sendable {
        public var id: Int
        public var poi: String?
        public var city: String?
        public var region: String?
        public var country: String?
        public var lat: Double?
        public var lon: Double?
        public var source: String
    }

    /// Only components the pipeline measures. Aesthetic and composition scores are absent by
    /// design; do not invent them client-side.
    public struct Quality: Codable, Equatable, Sendable {
        public var overall: Double?
        public var sharpness: Double?
        public var exposure: Double?
        public var contrast: Double?
        public var faceCount: Int?
        public var faceMaxFrac: Double?
        public var contentClass: String?

        private enum CodingKeys: String, CodingKey {
            case overall, sharpness, exposure, contrast
            case faceCount = "face_count"
            case faceMaxFrac = "face_max_frac"
            case contentClass = "content_class"
        }
    }

    /// Decoded fully — `{id, kind, is_keeper}` is what makes the alternates strip (I42)
    /// possible without a schema change. `id` is the keeper's `asset_id`, never a rowid.
    public struct Cluster: Codable, Equatable, Sendable {
        public var id: String?
        public var kind: Kind
        public var isKeeper: Bool

        private enum CodingKeys: String, CodingKey {
            case id, kind
            case isKeeper = "is_keeper"
        }

        public enum Kind: String, Codable, Equatable, Sendable {
            case exact
            case burst
            case similar
        }
    }

    public struct Selection: Codable, Equatable, Sendable {
        public var rank: Int
        public var reason: String?
    }

    public struct Video: Codable, Equatable, Sendable {
        public var durationSeconds: Double?
        public var fps: Double?
        public var poster: String?
        public var keyframes: [Keyframe]
        public var motionScore: Double?
        public var meanVolumeDb: Double?
        public var transcriptStatus: TranscriptStatus
        public var transcript: Transcript?
        public var subtype: Subtype
        public var storyboardCandidate: Bool

        private enum CodingKeys: String, CodingKey {
            case durationSeconds = "duration_seconds"
            case fps, poster, keyframes
            case motionScore = "motion_score"
            case meanVolumeDb = "mean_volume_db"
            case transcriptStatus = "transcript_status"
            case transcript, subtype
            case storyboardCandidate = "storyboard_candidate"
        }

        public struct Keyframe: Codable, Equatable, Sendable {
            public var seconds: Double
            public var path: String
        }

        /// `no_speech` means processed and nothing found; `not_processed` means never
        /// attempted. Do not collapse them.
        public enum TranscriptStatus: String, Codable, Equatable, Sendable {
            case transcribed
            case noSpeech = "no_speech"
            case notProcessed = "not_processed"
        }

        public struct Transcript: Codable, Equatable, Sendable {
            public var model: String
            public var text: String
            /// The schema leaves segment items unspecified, so they are carried verbatim.
            public var segments: [JSONValue]?
        }

        public enum Subtype: String, Codable, Equatable, Sendable {
            case clip
            case shortClip = "short_clip"
        }
    }

    /// Frame shape, for layout: a renderer that does not know this proposes a panoramic hero
    /// for a portrait photograph.
    public struct Geometry: Codable, Equatable, Sendable {
        public var width: Int?
        public var height: Int?
        public var orientation: Orientation?
        public var aspectRatio: Double?

        private enum CodingKeys: String, CodingKey {
            case width, height, orientation
            case aspectRatio = "aspect_ratio"
        }

        public enum Orientation: String, Codable, Equatable, Sendable {
            case landscape
            case portrait
            case square
        }
    }

    public struct Day: Codable, Equatable, Sendable {
        public var date: String
        public var events: [Event]
        public var counts: [String: Int]
        public var gpsCoverage: Double
        public var path: [Coordinate]
        public var highlights: [String]
        public var timezone: String?

        private enum CodingKeys: String, CodingKey {
            case date, events, counts
            case gpsCoverage = "gps_coverage"
            case path, highlights, timezone
        }
    }

    public struct Event: Codable, Equatable, Sendable {
        /// `<date>#<seq>`, never a rowid.
        public var id: String
        public var seq: Int
        public var label: String?
        public var startLocal: String?
        public var endLocal: String?
        public var durationMinutes: Double?
        public var place: Place?
        public var location: EventLocation
        public var counts: [String: Int]
        public var landmarks: [Landmark]
        public var highlights: [String]
        /// Asset ids, in order.
        public var assets: [String]
        public var durationSeconds: Int?

        private enum CodingKeys: String, CodingKey {
            case id, seq, label
            case startLocal = "start_local"
            case endLocal = "end_local"
            case durationMinutes = "duration_minutes"
            case place, location, counts, landmarks, highlights, assets
            case durationSeconds = "duration_seconds"
        }
    }

    /// One averaged coordinate can place an event where nobody stood, so the shape is
    /// described too.
    public struct EventLocation: Codable, Equatable, Sendable {
        public var centroid: Coordinate?
        public var first: Coordinate?
        public var last: Coordinate?
        public var radiusM: Double?
        public var gpsCoverage: Double
        public var path: [Coordinate]?

        private enum CodingKeys: String, CodingKey {
            case centroid, first, last
            case radiusM = "radius_m"
            case gpsCoverage = "gps_coverage"
            case path
        }
    }

    public struct Landmark: Codable, Equatable, Sendable {
        public var id: Int
        public var name: String
        public var confidence: Double?
        public var description: String?
        public var source: String
    }

    /// A `[lat, lon]` pair in the JSON; exactly two elements.
    public struct Coordinate: Codable, Equatable, Sendable {
        public var lat: Double
        public var lon: Double

        public init(lat: Double, lon: Double) {
            self.lat = lat
            self.lon = lon
        }

        public init(from decoder: Decoder) throws {
            var container = try decoder.unkeyedContainer()
            lat = try container.decode(Double.self)
            lon = try container.decode(Double.self)
            guard container.isAtEnd else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "coordinate has more than two elements")
            }
        }

        public func encode(to encoder: Encoder) throws {
            var container = encoder.unkeyedContainer()
            try container.encode(lat)
            try container.encode(lon)
        }
    }
}

extension TripDocument {
    /// Decode `trip.json` bytes.
    public static func decode(from data: Data) throws -> TripDocument {
        try JSONDecoder().decode(TripDocument.self, from: data)
    }
}

/// Arbitrary JSON, preserved verbatim where the contract leaves a shape unspecified
/// (transcript segments).
public indirect enum JSONValue: Codable, Equatable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "not a JSON value")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }
}
