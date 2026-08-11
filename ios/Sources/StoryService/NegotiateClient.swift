import Foundation
import StoryKit

/// I20. Tell the service which assets a trip has, by content hash, and get back **only what it
/// lacks** plus one presigned `PUT` per missing asset.
///
/// The acceptance criterion is a measurement: *a second upload of an unchanged trip transfers zero
/// bytes.* That falls out of hashing rather than out of bookkeeping — media is keyed by BLAKE2b
/// everywhere, so re-declaring an asset the store already holds is a no-op, and per-file retry,
/// resume and dedup are the same mechanism seen from three angles.
///
/// **Never a zip.** A ~600 MB blob has no resume, blocks the service until it lands, and re-sends
/// everything to add ten photographs. There is no route on the service that accepts media at all.
///
/// **The upload granularity is one asset; the unit of work stays the trip.** Negotiation batches
/// (the service bounds one request at 2000 assets) and uploads are per file, and neither divides
/// the work: one trip is one `story.db`, one build, one `trip.json`. T58 shipped the opposite —
/// input split per day, and three one-day `story.json` files came back.

// MARK: - What the client declares

public struct AssetDeclaration: Sendable, Equatable, Encodable {
    public let hash: String
    public let filename: String
    public let size: Int

    public enum DeclarationError: Error, Sendable, Equatable {
        /// The service rejects a short hash with 422 rather than silently matching nothing, and
        /// this rejects it one layer earlier with the same reasoning. `asset_id` is a *prefix* of
        /// the digest; sending the prefix would report every asset as needed forever.
        case hashIsNotFullDigest(String, length: Int)
        case hashIsNotLowercaseHex(String)
        case filenameIsEmpty
        case sizeIsNegative(Int)
    }

    /// 128 lowercase hex characters: `hashlib.blake2b()`'s default 64-byte digest, which is what
    /// `StoryKit.ContentHash` produces and what `scan.py` keys media by.
    public static let hashHexLength = 128

    public init(hash: String, filename: String, size: Int) throws {
        guard hash.count == Self.hashHexLength else {
            throw DeclarationError.hashIsNotFullDigest(hash, length: hash.count)
        }
        guard hash.allSatisfy({ $0.isHexDigit && !$0.isUppercase }) else {
            throw DeclarationError.hashIsNotLowercaseHex(hash)
        }
        guard !filename.isEmpty else { throw DeclarationError.filenameIsEmpty }
        guard size >= 0 else { throw DeclarationError.sizeIsNegative(size) }
        self.hash = hash
        self.filename = filename
        self.size = size
    }
}

// MARK: - What comes back

public struct UploadTicket: Sendable, Equatable {
    public let hash: String
    /// The original filename, preserved end to end because `overrides.toml` addresses media by it.
    public let filename: String
    /// What the *service* will call this file in the trip's source folder. Differs from `filename`
    /// only when two assets in one trip claim the same name, and then `filenameAdjusted` is true.
    /// The local file is still under `filename`.
    public let storedFilename: String
    public let filenameAdjusted: Bool
    public let size: Int
    public let method: String
    public let putURL: URL
    /// **Reproduce these exactly.** `Content-Length` is inside the signature, so a PUT that omits
    /// or changes it is rejected by S3 with a message about the signature and no hint why.
    public let headers: [String: String]
    public let expiresAtRaw: String
    /// `nil` when the service's timestamp did not parse. Then expiry is *unknown* — see
    /// ``isExpired(at:)``, which returns `nil` rather than `false`.
    public let expiresAt: Date?
    /// True when the store held an object of the wrong length at this key. The client is replacing
    /// something, and saying so beats a support conversation.
    public let replacesMismatchedObject: Bool

    /// `nil` means "cannot tell", and a caller must not read that as "still valid".
    public func isExpired(at now: Date) -> Bool? {
        guard let expiresAt else { return nil }
        return now >= expiresAt
    }

    /// Headers `URLSession` computes for itself and will not let a caller set.
    ///
    /// `Content-Length` is one of them: setting it on a `URLRequest` is ignored, and the value
    /// URLSession sends is the length of the body it was given. That is why ``UploadQueue``
    /// refuses to send a file whose length differs from `size` — matching the signed length is a
    /// property of the *file*, not of a header this client can write.
    public static let headersOwnedByURLSession: Set<String> = ["content-length"]

    public func urlRequest() -> URLRequest {
        var request = URLRequest(url: putURL)
        request.httpMethod = method
        for (name, value) in headers
        where !Self.headersOwnedByURLSession.contains(name.lowercased()) {
            request.setValue(value, forHTTPHeaderField: name)
        }
        return request
    }
}

public struct PresentAsset: Sendable, Equatable {
    public let hash: String
    public let size: Int
}

public struct Negotiation: Sendable, Equatable {
    public let tripID: String
    public let needed: [UploadTicket]
    /// What the store already holds. **`have` means "an object of the declared length is at that
    /// key"** — the service never reads the bytes, so it cannot confirm they hash to it, and it
    /// says so in `presenceNotVerified`. A disagreeing length is reported as needed, not believed.
    public let have: [PresentAsset]
    public let multipart: Bool
    public let multipartDetail: String?
    public let presenceNotVerified: String?

    /// The measurement the acceptance criterion is about: bytes this negotiation asks for.
    /// Zero on an unchanged trip.
    public var bytesToTransfer: Int { needed.reduce(0) { $0 + $1.size } }
}

public struct TripSummary: Sendable, Equatable {
    public let id: String
    public let name: String
    public let createdAtRaw: String
    public var createdAt: Date? { ServiceTime.date(from: createdAtRaw) }
}

public struct TripDetail: Sendable, Equatable {
    public struct DeclaredAsset: Sendable, Equatable {
        public let hash: String
        public let filename: String
        public let storedFilename: String
        public let size: Int
    }
    public let trip: TripSummary
    /// Every asset this trip has **declared**, uploaded or not — the service labels this
    /// `asset_scope: "declared"`. Presence is `source:prepare`'s answer, not this one.
    public let declaredAssets: [DeclaredAsset]
    public let assetScope: String?
}

public struct SourcePreparation: Sendable, Equatable {
    public let tripID: String
    public let declared: Int
    public let fetched: Int
    public let alreadyPresent: Int
    public let removed: [String]
    /// Hashes declared but not uploaded. While this is non-empty the service refuses to scaffold,
    /// because `story-book init` *measures* the folder and will not re-measure it later.
    public let missing: [String]
    public let complete: Bool
    public let scaffolded: Bool
    public let scaffoldDetail: String?
}

// MARK: - The client

public struct NegotiateClient: Sendable {
    /// The service bounds one negotiate request at 2000 assets. A larger set is split here and the
    /// answers merged — a request-size concern, never a division of the work.
    public static let maxAssetsPerRequest = 2000

    private let http: ServiceHTTP

    public init(
        endpoint: ServiceEndpoint,
        identity: any ServiceIdentity,
        session: URLSession = .shared
    ) {
        self.http = ServiceHTTP(endpoint: endpoint, identity: identity, session: session)
    }

    public func createTrip(name: String) async throws -> TripSummary {
        let wire = try await http.post("/trips", body: CreateTripBody(name: name), as: TripWire.self)
        return wire.summary
    }

    public func trips() async throws -> [TripSummary] {
        try await http.get("/trips", as: TripListWire.self).trips.map(\.summary)
    }

    public func trip(id: String) async throws -> TripDetail {
        let wire = try await http.get("/trips/\(id)", as: TripDetailWire.self)
        return TripDetail(
            trip: wire.summary,
            declaredAssets: wire.assets.map {
                .init(
                    hash: $0.hash, filename: $0.filename, storedFilename: $0.stored_filename,
                    size: $0.size)
            },
            assetScope: wire.asset_scope)
    }

    public func negotiate(tripID: String, assets: [AssetDeclaration]) async throws -> Negotiation {
        guard !assets.isEmpty else {
            // The service requires at least one asset. An empty local set is a caller bug, and a
            // fabricated empty `Negotiation` would look exactly like a satisfied trip.
            throw ServiceError.rejected(
                status: 422, detail: "negotiate at least one asset; the local set was empty")
        }
        // The service answers 422 for a repeated hash, and its index can hold one filename per
        // hash per trip anyway. Refused here rather than quietly collapsed, so the caller — which
        // knows what the second filename was — decides. `ExportFolder.scan` does that collapse and
        // reports what it dropped.
        var seen: Set<String> = []
        for asset in assets where !seen.insert(asset.hash).inserted {
            throw ServiceError.rejected(
                status: 422,
                detail:
                    "\(asset.filename) is byte-identical to another declared asset (\(asset.hash.prefix(12))…). "
                    + "One hash may appear once per negotiate; use ExportFolder.scan, which collapses "
                    + "duplicates and reports the filenames it did not declare.")
        }

        var needed: [UploadTicket] = []
        var have: [PresentAsset] = []
        var multipart = false
        var multipartDetail: String?
        var presenceNotVerified: String?
        var resolvedTripID = tripID

        for batch in assets.chunked(into: Self.maxAssetsPerRequest) {
            let wire = try await http.post(
                "/trips/\(tripID)/assets:negotiate",
                body: NegotiateBody(assets: batch),
                as: NegotiateWire.self)
            resolvedTripID = wire.trip_id
            needed.append(contentsOf: try wire.needed.map { try $0.ticket() })
            have.append(contentsOf: wire.have.map { PresentAsset(hash: $0.hash, size: $0.size) })
            multipart = multipart || (wire.upload?.multipart ?? false)
            multipartDetail = wire.upload?.multipart_detail ?? multipartDetail
            presenceNotVerified = wire.upload?.presence_not_verified ?? presenceNotVerified
        }

        return Negotiation(
            tripID: resolvedTripID,
            needed: needed,
            have: have,
            multipart: multipart,
            multipartDetail: multipartDetail,
            presenceNotVerified: presenceNotVerified)
    }

    /// Fetch the uploaded media onto the service's filesystem and scaffold the trip's config.
    ///
    /// Idempotent, and a build calls it as step zero anyway — but it is worth calling directly
    /// after an upload run, because `missing` is the only place that answers "did every byte
    /// arrive?" before a build starts on a short trip.
    public func prepareSource(tripID: String) async throws -> SourcePreparation {
        let wire = try await http.post("/trips/\(tripID)/source:prepare", as: PrepareWire.self)
        return SourcePreparation(
            tripID: wire.trip_id,
            declared: wire.declared,
            fetched: wire.fetched,
            alreadyPresent: wire.already_present,
            removed: wire.removed,
            missing: wire.missing,
            complete: wire.complete,
            scaffolded: wire.scaffolded ?? false,
            scaffoldDetail: wire.scaffold_detail)
    }
}

// MARK: - Declaring an export folder

/// Turn a folder written by `FolderWriter` into declarations.
///
/// The folder holds the media, one `<name>.sidecar.json` per item, and `export_ledger.json`. Only
/// the media is declared: the pipeline's scanner ignores the other two, and so does this.
public enum ExportFolder {
    public enum ScanError: Error, Sendable, Equatable {
        case notADirectory(String)
    }

    /// What a folder holds, with **byte-identical duplicates collapsed to one declaration**.
    ///
    /// A real library contains them — the committed fixture set has `exact_a.jpg` / `exact_b.jpg`
    /// precisely because the pipeline's dedup exists to handle them — and the service rejects a
    /// negotiate in which one hash appears twice (422, found by running the loop over the whole
    /// fixture folder). Its index is keyed `(trip_id, media_hash)`, so it can hold exactly one
    /// filename per hash whatever the client sends.
    ///
    /// So the collapse happens here, visibly, and the names it did not declare are **reported
    /// rather than dropped**: `overrides.toml` addresses media by filename, and a correction naming
    /// `exact_b.jpg` will not resolve on the service. See the tracker's cross-task request XT-3.
    public struct Scan: Sendable, Equatable {
        /// One per distinct hash, ordered by filename.
        public let declarations: [AssetDeclaration]
        /// hash → filenames of further copies, which no upload and no `trip.json` will mention.
        /// Empty for a folder with no byte-identical duplicates.
        public let undeclaredDuplicates: [String: [String]]

        public var hasUndeclaredDuplicates: Bool { !undeclaredDuplicates.isEmpty }
    }

    /// - Parameters:
    ///   - folder: the export folder.
    ///   - ledger: the export ledger, when the caller has it. Its hashes are reused rather than
    ///     recomputed — `FolderWriter` saves the ledger only *after* a complete write, so an entry
    ///     is evidence the file finished, whereas the file's existence is not.
    ///   - rehash: hash every file from disk even when the ledger has an entry. Slower by the cost
    ///     of reading the whole folder; use it when the folder may have been touched by something
    ///     other than the writer.
    /// The declarations alone, duplicates collapsed. Use ``scan(in:ledger:rehash:)`` when the caller
    /// should know that a copy was collapsed — which the app's upload screen should.
    public static func declarations(
        in folder: URL,
        ledger: ExportLedger? = nil,
        rehash: Bool = false
    ) throws -> [AssetDeclaration] {
        try scan(in: folder, ledger: ledger, rehash: rehash).declarations
    }

    public static func scan(
        in folder: URL,
        ledger: ExportLedger? = nil,
        rehash: Bool = false
    ) throws -> Scan {
        let fm = FileManager.default
        guard
            let values = try? folder.resourceValues(forKeys: [.isDirectoryKey]),
            values.isDirectory == true
        else {
            throw ScanError.notADirectory(folder.path)
        }

        let hashByFilename = Dictionary(
            (ledger?.entries ?? []).map { ($0.originalFilename, $0.contentHash) },
            // Byte-identical duplicates legitimately share a hash; two entries under one filename
            // would be the same file exported twice, so either answer is the same answer.
            uniquingKeysWith: { first, _ in first })

        var declarations: [AssetDeclaration] = []
        for name in try fm.contentsOfDirectory(atPath: folder.path).sorted() {
            if name.hasPrefix(".") { continue }
            if name == ExportLedgerFile.defaultFileName { continue }
            if name.hasSuffix(".sidecar.json") { continue }
            let url = folder.appending(path: name)
            let attributes = try fm.attributesOfItem(atPath: url.path)
            guard (attributes[.type] as? FileAttributeType) == .typeRegular else { continue }
            let size = (attributes[.size] as? NSNumber)?.intValue ?? 0
            let hash: String
            if !rehash, let known = hashByFilename[name] {
                hash = known
            } else {
                hash = try ContentHash.hex(of: url)
            }
            declarations.append(try AssetDeclaration(hash: hash, filename: name, size: size))
        }

        var declared: [String: AssetDeclaration] = [:]
        var duplicates: [String: [String]] = [:]
        for declaration in declarations {
            if declared[declaration.hash] == nil {
                declared[declaration.hash] = declaration
            } else {
                duplicates[declaration.hash, default: []].append(declaration.filename)
            }
        }
        return Scan(
            declarations: declared.values.sorted { $0.filename < $1.filename },
            undeclaredDuplicates: duplicates)
    }
}

// MARK: - Wire shapes

/// Decoded with explicit keys and no `convertFromSnakeCase`, following I03: that strategy also
/// rewrites data-bearing dictionary keys, and `headers` is a dictionary whose keys are HTTP header
/// names — `Content-Length` must not become `contentLength`.
private struct CreateTripBody: Encodable, Sendable {
    let name: String
}

private struct NegotiateBody: Encodable, Sendable {
    let assets: [AssetDeclaration]
}

private struct TripWire: Decodable, Sendable {
    let trip_id: String
    let name: String
    let created_at: String

    var summary: TripSummary {
        TripSummary(id: trip_id, name: name, createdAtRaw: created_at)
    }
}

private struct TripListWire: Decodable, Sendable {
    let trips: [TripWire]
}

private struct TripDetailWire: Decodable, Sendable {
    struct Asset: Decodable, Sendable {
        let hash: String
        let filename: String
        let stored_filename: String
        let size: Int
    }
    let trip_id: String
    let name: String
    let created_at: String
    let assets: [Asset]
    let asset_scope: String?

    var summary: TripSummary {
        TripSummary(id: trip_id, name: name, createdAtRaw: created_at)
    }
}

private struct NegotiateWire: Decodable, Sendable {
    struct Needed: Decodable, Sendable {
        let hash: String
        let filename: String
        let stored_filename: String
        let filename_adjusted: Bool
        let size: Int
        let method: String
        let put_url: String
        let headers: [String: String]
        let expires_at: String
        let replaces_mismatched_object: Bool

        func ticket() throws -> UploadTicket {
            guard let url = URL(string: put_url) else {
                throw ServiceError.malformedResponse("put_url is not a URL for \(hash)")
            }
            return UploadTicket(
                hash: hash,
                filename: filename,
                storedFilename: stored_filename,
                filenameAdjusted: filename_adjusted,
                size: size,
                method: method,
                putURL: url,
                headers: headers,
                expiresAtRaw: expires_at,
                expiresAt: ServiceTime.date(from: expires_at),
                replacesMismatchedObject: replaces_mismatched_object)
        }
    }
    struct Have: Decodable, Sendable {
        let hash: String
        let size: Int
    }
    struct Upload: Decodable, Sendable {
        let multipart: Bool?
        let multipart_detail: String?
        let presence_not_verified: String?
    }
    let trip_id: String
    let needed: [Needed]
    let have: [Have]
    let upload: Upload?
}

private struct PrepareWire: Decodable, Sendable {
    let trip_id: String
    let declared: Int
    let fetched: Int
    let already_present: Int
    let removed: [String]
    let missing: [String]
    let complete: Bool
    let scaffolded: Bool?
    let scaffold_detail: String?
}

extension Array {
    func chunked(into size: Int) -> [[Element]] {
        guard size > 0, count > size else { return isEmpty ? [] : [self] }
        return stride(from: 0, to: count, by: size).map {
            Array(self[$0..<Swift.min($0 + size, count)])
        }
    }
}
