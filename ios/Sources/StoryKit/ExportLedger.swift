import Foundation

/// The local, durable map `PHAsset.localIdentifier` ↔ content hash ↔ `originalFilename`.
///
/// `asset_id` is a prefix of the BLAKE2b of the *exported* bytes, so export time is the only
/// moment anything knows which `PHAsset` produced which hash (D6). If it is not recorded here
/// it cannot be rebuilt without re-exporting the whole library. This is what lets the report
/// resolve an `asset_id` back to the original on the phone (I25).
public struct ExportLedger: Equatable, Sendable {
    public struct Entry: Codable, Equatable, Sendable {
        public var localIdentifier: String
        /// 128-character BLAKE2b hex of the exported bytes, as computed by `ContentHash`.
        public var contentHash: String
        public var originalFilename: String

        public init(localIdentifier: String, contentHash: String, originalFilename: String) {
            self.localIdentifier = localIdentifier
            self.contentHash = contentHash
            self.originalFilename = originalFilename
        }

        private enum CodingKeys: String, CodingKey {
            case localIdentifier = "local_identifier"
            case contentHash = "content_hash"
            case originalFilename = "original_filename"
        }
    }

    private var entriesByLocalIdentifier: [String: Entry] = [:]

    public init() {}

    public var count: Int { entriesByLocalIdentifier.count }

    public var entries: [Entry] {
        entriesByLocalIdentifier.values.sorted { $0.localIdentifier < $1.localIdentifier }
    }

    /// Upsert by `localIdentifier`: re-exporting an asset whose bytes changed (an edit, a new
    /// export recipe) replaces its hash rather than leaving two claims about one asset.
    public mutating func record(_ entry: Entry) {
        entriesByLocalIdentifier[entry.localIdentifier] = entry
    }

    public func entry(forLocalIdentifier id: String) -> Entry? {
        entriesByLocalIdentifier[id]
    }

    /// All entries whose hash begins with `assetId`. A prefix can legitimately match several
    /// entries (byte-identical duplicates in the library), so the caller chooses; any of them
    /// is the same picture.
    public func entries(matchingAssetId assetId: String) -> [Entry] {
        entries.filter { $0.contentHash.hasPrefix(assetId) }
    }
}

/// File persistence, kept apart from the value type so tests can exercise the ledger without
/// touching disk. Writes are atomic: a relaunch mid-save sees the previous complete ledger,
/// never a torn file.
public enum ExportLedgerFile {
    public static let defaultFileName = "export_ledger.json"

    private struct Envelope: Codable {
        var schemaVersion: Int
        var entries: [ExportLedger.Entry]

        private enum CodingKeys: String, CodingKey {
            case schemaVersion = "schema_version"
            case entries
        }
    }

    /// A missing file is an empty ledger — first launch, not an error.
    public static func load(from url: URL) throws -> ExportLedger {
        guard FileManager.default.fileExists(atPath: url.path) else {
            return ExportLedger()
        }
        let envelope = try JSONDecoder().decode(Envelope.self, from: Data(contentsOf: url))
        var ledger = ExportLedger()
        for entry in envelope.entries {
            ledger.record(entry)
        }
        return ledger
    }

    public static func save(_ ledger: ExportLedger, to url: URL) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(
            Envelope(schemaVersion: 1, entries: ledger.entries))
        try data.write(to: url, options: .atomic)
    }
}
