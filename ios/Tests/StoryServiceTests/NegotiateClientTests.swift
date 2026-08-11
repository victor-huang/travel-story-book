import Foundation
import StoryKit
import Testing

@testable import StoryService

struct AssetDeclarationTests {
    private let full = String(repeating: "ab", count: 64)

    @Test func acceptsAFullDigest() throws {
        let declaration = try AssetDeclaration(hash: full, filename: "IMG_0001.HEIC", size: 12)
        #expect(declaration.hash.count == 128)
    }

    /// `asset_id` is a *prefix* of the digest. The service answers 422 for one; this refuses it a
    /// layer earlier, because the failure it prevents is silent — every asset needed forever.
    @Test func refusesAnAssetIdPrefix() {
        #expect(throws: AssetDeclaration.DeclarationError.hashIsNotFullDigest(String(full.prefix(16)), length: 16)) {
            _ = try AssetDeclaration(hash: String(full.prefix(16)), filename: "a.jpg", size: 1)
        }
    }

    @Test func refusesAnUppercaseDigest() {
        let upper = full.uppercased()
        #expect(throws: AssetDeclaration.DeclarationError.hashIsNotLowercaseHex(upper)) {
            _ = try AssetDeclaration(hash: upper, filename: "a.jpg", size: 1)
        }
    }

    @Test func refusesAnEmptyFilename() {
        #expect(throws: AssetDeclaration.DeclarationError.filenameIsEmpty) {
            _ = try AssetDeclaration(hash: full, filename: "", size: 1)
        }
    }
}

struct ServiceEndpointTests {
    /// Two routes carry a colon inside a path segment. A percent-encoded colon is a different path
    /// and the service answers 404, which would read as "no such trip".
    @Test func keepsTheColonInAssetsNegotiate() {
        let endpoint = ServiceEndpoint(baseURL: URL(string: "https://example.invalid")!)
        #expect(
            endpoint.url("/trips/t1/assets:negotiate").absoluteString
                == "https://example.invalid/trips/t1/assets:negotiate")
    }

    @Test func toleratesATrailingSlashOnTheBaseURL() {
        let endpoint = ServiceEndpoint(baseURL: URL(string: "http://127.0.0.1:8000/")!)
        #expect(endpoint.url("/trips").absoluteString == "http://127.0.0.1:8000/trips")
    }
}

struct ServiceTimeTests {
    /// `datetime.isoformat()` writes six fractional digits. This is the shape the service actually
    /// sends, taken from its own responses.
    @Test func parsesPythonsSixDigitFraction() {
        #expect(ServiceTime.date(from: "2026-08-10T09:00:00.123456+00:00") != nil)
    }

    @Test func parsesATimestampWithNoFraction() {
        #expect(ServiceTime.date(from: "2026-08-10T09:00:00+00:00") != nil)
    }

    /// The control: nonsense must not parse, or the two tests above prove only that the parser
    /// returns something.
    @Test func refusesSomethingThatIsNotATimestamp() {
        #expect(ServiceTime.date(from: "soon") == nil)
    }
}

struct UploadTicketTests {
    private func ticket(expiry: String) -> UploadTicket {
        UploadTicket(
            hash: String(repeating: "0a", count: 64), filename: "a.jpg", storedFilename: "a.jpg",
            filenameAdjusted: false, size: 3, method: "PUT",
            putURL: URL(string: "https://store.invalid/k")!,
            headers: ["Content-Length": "3"], expiresAtRaw: expiry,
            expiresAt: ServiceTime.date(from: expiry), replacesMismatchedObject: false)
    }

    @Test func anExpiredGrantIsExpired() {
        #expect(ticket(expiry: "2020-01-01T00:00:00+00:00").isExpired(at: Date()) == true)
    }

    @Test func aFutureGrantIsNotExpired() {
        #expect(ticket(expiry: "2099-01-01T00:00:00+00:00").isExpired(at: Date()) == false)
    }

    /// An unparsable expiry is *unknown*, and must not read as "still valid". This is the same rule
    /// as `keptFraction` being nil rather than 0.0 in I10.
    @Test func anUnparsableExpiryIsUnknownRatherThanValid() {
        #expect(ticket(expiry: "whenever").isExpired(at: Date()) == nil)
    }

    /// `Content-Length` is inside the signature and `URLSession` computes it from the body, so the
    /// request must not carry a hand-written one — the file's own length is what has to match.
    @Test func doesNotSetContentLengthOnTheRequest() {
        let request = ticket(expiry: "2099-01-01T00:00:00+00:00").urlRequest()
        #expect(request.httpMethod == "PUT")
        #expect(request.value(forHTTPHeaderField: "Content-Length") == nil)
    }
}

struct NegotiateClientTests {
    private func hash(_ byte: String) -> String { String(repeating: byte, count: 64) }

    @Test func createsATripAndReadsItsId() async throws {
        let fake = FakeService()
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        let client = NegotiateClient(endpoint: endpoint, identity: identity, session: session)

        let trip = try await client.createTrip(name: "Europe")
        #expect(trip.id == "trip-1")
        #expect(trip.createdAt != nil)
    }

    @Test func sendsTheIdentityHeaderOnEveryRequest() async throws {
        let fake = FakeService()
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        _ = try await NegotiateClient(endpoint: endpoint, identity: identity, session: session)
            .createTrip(name: "Europe")
        #expect(StubHTTP.recorded(host: fake.host).allSatisfy {
            $0.headers["X-Story-Identity"] == "traveller@example.com"
        })
    }

    /// The control for the test above: without the header the service answers 401, and this client
    /// must surface that as `unauthorized` rather than as a generic failure.
    @Test func reportsAMissingIdentityAsUnauthorized() async throws {
        struct NoIdentity: ServiceIdentity {
            func identityHeaders() async throws -> [String: String] { [:] }
        }
        let fake = FakeService()
        let (endpoint, session, _) = fake.install()
        defer { fake.uninstall() }
        let client = NegotiateClient(endpoint: endpoint, identity: NoIdentity(), session: session)

        await #expect(throws: ServiceError.unauthorized("no caller identity")) {
            _ = try await client.createTrip(name: "Europe")
        }
    }

    @Test func returnsATicketPerMissingAsset() async throws {
        let fake = FakeService()
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        let client = NegotiateClient(endpoint: endpoint, identity: identity, session: session)

        let negotiation = try await client.negotiate(
            tripID: "trip-1",
            assets: [
                try AssetDeclaration(hash: hash("0a"), filename: "a.jpg", size: 10),
                try AssetDeclaration(hash: hash("0b"), filename: "b.jpg", size: 20),
            ])

        #expect(negotiation.needed.map(\.hash).sorted() == [hash("0a"), hash("0b")].sorted())
        #expect(negotiation.bytesToTransfer == 30)
        #expect(negotiation.multipart == false)
    }

    /// I20's criterion at the level of the negotiation alone: what the store holds is not asked for.
    /// Its control is the test above, where the same call over an empty store asks for 30 bytes.
    @Test func asksForNothingTheStoreAlreadyHolds() async throws {
        let fake = FakeService()
        fake.seedStore(hash: hash("0a"), size: 10)
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        let client = NegotiateClient(endpoint: endpoint, identity: identity, session: session)

        let negotiation = try await client.negotiate(
            tripID: "trip-1",
            assets: [try AssetDeclaration(hash: hash("0a"), filename: "a.jpg", size: 10)])

        #expect(negotiation.needed.isEmpty)
        #expect(negotiation.have == [PresentAsset(hash: hash("0a"), size: 10)])
        #expect(negotiation.bytesToTransfer == 0)
    }

    /// A stored object of the wrong length is reported as needed, and the ticket says it is
    /// replacing something. The service cannot verify the bytes hash to the key, so a disagreeing
    /// length is the one contradiction it can act on.
    @Test func reUploadsWhenTheStoredLengthDisagrees() async throws {
        let fake = FakeService()
        fake.seedStore(hash: hash("0a"), size: 7)
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        let client = NegotiateClient(endpoint: endpoint, identity: identity, session: session)

        let negotiation = try await client.negotiate(
            tripID: "trip-1",
            assets: [try AssetDeclaration(hash: hash("0a"), filename: "a.jpg", size: 10)])

        #expect(negotiation.needed.count == 1)
        #expect(negotiation.needed[0].replacesMismatchedObject == true)
    }

    @Test func refusesToInventAnEmptyNegotiation() async throws {
        let fake = FakeService()
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        let client = NegotiateClient(endpoint: endpoint, identity: identity, session: session)

        await #expect(throws: ServiceError.self) {
            _ = try await client.negotiate(tripID: "trip-1", assets: [])
        }
        // And it did not go to the network to find that out.
        #expect(StubHTTP.recorded(host: fake.host).isEmpty)
    }

    /// Refused locally, with a message naming the file and the way out. The service's own 422 for
    /// this case is a 500-character validation dump that names neither.
    @Test func refusesTwoDeclarationsOfOneHashBeforeSendingThem() async throws {
        let fake = FakeService()
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        let client = NegotiateClient(endpoint: endpoint, identity: identity, session: session)

        await #expect(throws: ServiceError.self) {
            _ = try await client.negotiate(
                tripID: "trip-1",
                assets: [
                    try AssetDeclaration(hash: hash("0a"), filename: "exact_a.jpg", size: 4),
                    try AssetDeclaration(hash: hash("0a"), filename: "exact_b.jpg", size: 4),
                ])
        }
        #expect(StubHTTP.recorded(host: fake.host).isEmpty)
    }

    @Test func chunkingIsTheRequestSizeAndNotTheUnitOfWork() throws {
        let assets = (0..<4501).map { _ in 1 }
        let batches = assets.chunked(into: NegotiateClient.maxAssetsPerRequest)
        #expect(batches.map(\.count) == [2000, 2000, 501])
    }
}

struct ExportFolderTests {
    private func folder() throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appending(
            path: "export-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    @Test func declaresTheMediaAndNothingElse() throws {
        let dir = try folder()
        defer { try? FileManager.default.removeItem(at: dir) }
        try Data("photo".utf8).write(to: dir.appending(path: "IMG_0001.jpg"))
        try Data("{}".utf8).write(to: dir.appending(path: "IMG_0001.jpg.sidecar.json"))
        try Data("{}".utf8).write(to: dir.appending(path: ExportLedgerFile.defaultFileName))

        let declarations = try ExportFolder.declarations(in: dir)
        #expect(declarations.map(\.filename) == ["IMG_0001.jpg"])
        #expect(declarations[0].size == 5)
        #expect(declarations[0].hash == ContentHash.hex(of: Data("photo".utf8)))
    }

    /// The ledger's hash is reused because `FolderWriter` writes the ledger only *after* a complete
    /// export, so an entry is evidence the file finished. Presence is not.
    @Test func reusesTheLedgersHashRatherThanRereadingTheFile() throws {
        let dir = try folder()
        defer { try? FileManager.default.removeItem(at: dir) }
        try Data("photo".utf8).write(to: dir.appending(path: "IMG_0001.jpg"))
        var ledger = ExportLedger()
        let recorded = String(repeating: "0c", count: 64)
        ledger.record(
            .init(
                localIdentifier: "L0/001", contentHash: recorded,
                originalFilename: "IMG_0001.jpg"))

        let fromLedger = try ExportFolder.declarations(in: dir, ledger: ledger)
        #expect(fromLedger[0].hash == recorded)

        // The control: rehashing reads the file and disagrees, so the first result really did come
        // from the ledger and not from the bytes.
        let rehashed = try ExportFolder.declarations(in: dir, ledger: ledger, rehash: true)
        #expect(rehashed[0].hash != recorded)
        #expect(rehashed[0].hash == ContentHash.hex(of: Data("photo".utf8)))
    }

    /// A real library holds byte-identical duplicates — `tests/fixtures/media/` has a pair on
    /// purpose. The service rejects a negotiate with one hash twice, and its index is keyed
    /// `(trip_id, media_hash)`, so the collapse happens here and the name it did not declare is
    /// reported rather than dropped.
    @Test func collapsesByteIdenticalDuplicatesAndSaysWhichNameItDropped() throws {
        let dir = try folder()
        defer { try? FileManager.default.removeItem(at: dir) }
        try Data("same".utf8).write(to: dir.appending(path: "exact_a.jpg"))
        try Data("same".utf8).write(to: dir.appending(path: "exact_b.jpg"))
        try Data("other".utf8).write(to: dir.appending(path: "distinct.jpg"))

        let scan = try ExportFolder.scan(in: dir)
        #expect(scan.declarations.map(\.filename) == ["distinct.jpg", "exact_a.jpg"])
        #expect(scan.undeclaredDuplicates.values.flatMap { $0 } == ["exact_b.jpg"])
    }

    /// The control: a folder with no duplicates reports none, so the field above is not always full.
    @Test func aFolderWithNoDuplicatesReportsNone() throws {
        let dir = try folder()
        defer { try? FileManager.default.removeItem(at: dir) }
        try Data("a".utf8).write(to: dir.appending(path: "a.jpg"))
        try Data("b".utf8).write(to: dir.appending(path: "b.jpg"))
        let scan = try ExportFolder.scan(in: dir)
        #expect(scan.declarations.count == 2)
        #expect(scan.hasUndeclaredDuplicates == false)
    }

    @Test func refusesAPathThatIsNotADirectory() throws {
        let dir = try folder()
        defer { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appending(path: "a.jpg")
        try Data("x".utf8).write(to: file)
        #expect(throws: ExportFolder.ScanError.notADirectory(file.path)) {
            _ = try ExportFolder.declarations(in: file)
        }
    }
}
