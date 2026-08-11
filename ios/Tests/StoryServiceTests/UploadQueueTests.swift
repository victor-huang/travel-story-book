import Foundation
import Testing

@testable import StoryService

/// Helpers shared by the queue suites.
struct UploadFixture {
    let directory: URL
    let fake: FakeService
    let endpoint: ServiceEndpoint
    let session: URLSession
    let identity: any ServiceIdentity

    init() throws {
        directory = FileManager.default.temporaryDirectory.appending(
            path: "upload-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        fake = FakeService()
        (endpoint, session, identity) = fake.install()
    }

    func tearDown() {
        fake.uninstall()
        try? FileManager.default.removeItem(at: directory)
    }

    var stateURL: URL { directory.appending(path: "upload_queue.json") }

    var negotiateClient: NegotiateClient {
        NegotiateClient(endpoint: endpoint, identity: identity, session: session)
    }

    /// Write a file of `bytes` bytes and return its declaration, hashed for real.
    @discardableResult
    func write(_ name: String, bytes: Int) throws -> AssetDeclaration {
        let url = directory.appending(path: name)
        try Data(repeating: UInt8(name.utf8.first ?? 65), count: bytes).write(to: url)
        return try ExportFolder.declarations(in: directory).first { $0.filename == name }!
    }

    func declarations() throws -> [AssetDeclaration] {
        try ExportFolder.declarations(in: directory)
    }

    func transport() -> URLSessionUploadTransport {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubHTTP.self]
        return URLSessionUploadTransport(configuration: configuration)
    }

    func queue(transport: any UploadTransport, maxAttempts: Int = 3) throws -> UploadQueue {
        try UploadQueue(
            stateURL: stateURL, transport: transport, maxAttempts: maxAttempts,
            retryDelay: .zeroForTests)
    }
}

extension Duration {
    /// Retries in tests must not actually wait. Named so a reader sees the choice.
    static var zeroForTests: Duration { .milliseconds(0) }
}

struct UploadQueueTests {
    @Test func putsEachAssetStraightToTheStoreURL() async throws {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 100)
        try fixture.write("b.jpg", bytes: 250)

        let negotiation = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: try fixture.declarations())
        let queue = try fixture.queue(transport: fixture.transport())
        await queue.enqueue(negotiation.needed, filesIn: fixture.directory)
        let report = await queue.run()

        #expect(report.uploaded.count == 2)
        #expect(report.isComplete)
        #expect(report.assetsSent == 2)
        // The bytes the *store* received, counted on the other side of the wire. This is the only
        // measurement in this suite that is a measurement — see the test below for why the client's
        // own count is nil here.
        #expect(fixture.fake.bytesPut == 350)
    }

    /// **The finding that changed the design.** A `URLSession` load served by a custom
    /// `URLProtocol` reports `countOfBytesSent == 0` and sends no progress callbacks, so a client
    /// that trusted it would have claimed *zero bytes transferred* for an upload of 350 — and
    /// "a second upload transfers zero bytes" would then have been true of the first upload too.
    /// The client reports `nil` for unmeasured rather than a zero that reads like a reading.
    @Test func reportsAnUnmeasurableTransferAsUnknownRatherThanZero() async throws {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 100)
        let negotiation = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: try fixture.declarations())
        let queue = try fixture.queue(transport: fixture.transport())
        await queue.enqueue(negotiation.needed, filesIn: fixture.directory)
        let report = await queue.run()

        #expect(report.assetsSent == 1)
        #expect(report.bytesTransferred == nil)
        // The control: bytes really did move, so `nil` means "not counted", not "nothing sent".
        #expect(fixture.fake.bytesPut == 100)
    }

    /// The PUT goes to the presigned URL and carries **no identity header** — the API server never
    /// sees the bytes, and S3 would not know what to do with the header if it did.
    @Test func theUploadDoesNotGoThroughTheApi() async throws {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 100)

        let negotiation = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: try fixture.declarations())
        let queue = try fixture.queue(transport: fixture.transport())
        await queue.enqueue(negotiation.needed, filesIn: fixture.directory)
        _ = await queue.run()

        let puts = StubHTTP.recorded(host: fixture.fake.host).filter { $0.method == "PUT" }
        #expect(puts.count == 1)
        #expect(puts[0].url.path.hasPrefix("/store/"))
        #expect(puts[0].headers["X-Story-Identity"] == nil)
    }

    /// **I20's acceptance criterion, with its control.** The first upload transfers every byte; the
    /// second, over an unchanged folder, transfers none — and issues no PUT at all. Without the
    /// first half this passes against a client that uploads nothing.
    @Test func aSecondUploadOfAnUnchangedTripTransfersZeroBytes() async throws {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 100)
        try fixture.write("b.jpg", bytes: 250)
        let declarations = try fixture.declarations()

        let first = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: declarations)
        #expect(first.bytesToTransfer == 350)
        let queue = try fixture.queue(transport: fixture.transport())
        await queue.enqueue(first.needed, filesIn: fixture.directory)
        let firstReport = await queue.run()
        #expect(firstReport.assetsSent == 2)
        // Measured where a measurement is possible: on the store's side of the wire.
        #expect(fixture.fake.bytesPut == 350)

        // A fresh launch: new queue, new state read from disk, new negotiation.
        let second = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: declarations)
        #expect(second.needed.isEmpty)
        #expect(second.have.count == 2)
        let relaunched = try fixture.queue(transport: fixture.transport())
        await relaunched.enqueue(second.needed, filesIn: fixture.directory)
        let secondReport = await relaunched.run()

        // Zero, and it is a zero the client is entitled to: nothing was sent, so nothing went
        // unmeasured. Contrast the first run, which reports `nil`.
        #expect(secondReport.assetsSent == 0)
        #expect(secondReport.bytesTransferred == 0)
        #expect(fixture.fake.bytesPut == 350)  // unchanged by the second run
        #expect(fixture.fake.putCount == 2)  // still two, from the first run only
    }

    /// **I21's acceptance criterion.** The third asset is refused, the process "dies", and a fresh
    /// queue over the same state file re-sends only that one.
    @Test func aRelaunchResumesWithoutResendingCompletedAssets() async throws {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 100)
        try fixture.write("b.jpg", bytes: 200)
        try fixture.write("c.jpg", bytes: 400)
        let declarations = try fixture.declarations()
        let doomed = declarations.first { $0.filename == "c.jpg" }!

        // c.jpg is refused with a 500 for the whole first launch: transient, so it is retried and
        // then left as failed rather than forgotten.
        fixture.fake.putStatus[doomed.hash] = 500

        let first = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: declarations)
        let queue = try fixture.queue(transport: fixture.transport(), maxAttempts: 2)
        await queue.enqueue(first.needed, filesIn: fixture.directory)
        let firstReport = await queue.run()
        #expect(firstReport.uploaded.count == 2)
        #expect(firstReport.failed.keys.contains(doomed.hash))
        let bytesFirstLaunch = fixture.fake.bytesPut

        // The relaunch. Nothing is shared with the queue above but the state file on disk.
        fixture.fake.putStatus[doomed.hash] = nil
        let second = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: declarations)
        #expect(second.needed.map(\.hash) == [doomed.hash])
        let relaunched = try fixture.queue(transport: fixture.transport())
        await relaunched.enqueue(second.needed, filesIn: fixture.directory)
        let secondReport = await relaunched.run()

        #expect(secondReport.uploaded == [doomed.hash])
        // Only c.jpg's 400 bytes moved in the second launch, and a and b were not touched again.
        #expect(fixture.fake.bytesPut - bytesFirstLaunch == 400)
        #expect(fixture.fake.bytesPut(for: declarations.first { $0.filename == "a.jpg" }!.hash) == 100)
    }

    /// The durable half, on its own: even when a stale ticket is re-enqueued, an asset this queue
    /// already finished is not sent again. This is what covers the launch where the network is down
    /// and no negotiation has happened yet.
    @Test func theStateFileAloneStopsAResend() async throws {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 100)
        let declarations = try fixture.declarations()
        let negotiation = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: declarations)

        let queue = try fixture.queue(transport: fixture.transport())
        await queue.enqueue(negotiation.needed, filesIn: fixture.directory)
        _ = await queue.run()

        // Same tickets, fresh queue: the store would happily take them again.
        let relaunched = try fixture.queue(transport: fixture.transport())
        await relaunched.enqueue(negotiation.needed, filesIn: fixture.directory)
        let report = await relaunched.run()

        #expect(report.alreadySentEarlier == negotiation.needed.map(\.hash))
        #expect(report.bytesTransferred == 0)
        #expect(fixture.fake.putCount == 1)
    }

    @Test func recordsCompletionAfterEveryAssetRatherThanAtTheEnd() async throws {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 10)
        try fixture.write("b.jpg", bytes: 10)
        let declarations = try fixture.declarations()
        // b is permanently refused, so the run ends with one success and one failure. If the state
        // were written only at the end of a successful run, a's completion would be lost.
        fixture.fake.putStatus[declarations[1].hash] = 400

        let negotiation = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: declarations)
        let queue = try fixture.queue(transport: fixture.transport())
        await queue.enqueue(negotiation.needed, filesIn: fixture.directory)
        _ = await queue.run()

        let reloaded = try fixture.queue(transport: fixture.transport())
        #expect(await reloaded.entry(for: declarations[0].hash)?.state == .uploaded)
        #expect(await reloaded.entry(for: declarations[1].hash)?.state == .needsRenegotiation)
    }

    @Test func retriesATransientFailureAndSucceeds() async throws {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 64)
        let declarations = try fixture.declarations()
        fixture.fake.failPutOnce = [declarations[0].hash]

        let negotiation = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: declarations)
        let queue = try fixture.queue(transport: fixture.transport())
        await queue.enqueue(negotiation.needed, filesIn: fixture.directory)
        let report = await queue.run()

        #expect(report.uploaded == [declarations[0].hash])
        #expect(await queue.entry(for: declarations[0].hash)?.attempts == 2)
    }

    /// A 403 is a spent grant, not a transient fault: retrying the same URL is a slower way to
    /// fail, so the queue asks for a new one instead. The state name is what the app acts on.
    @Test func aRefusedSignatureAsksForRenegotiationRatherThanRetrying()
        async throws
    {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 64)
        let declarations = try fixture.declarations()
        fixture.fake.putStatus[declarations[0].hash] = 403

        let negotiation = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: declarations)
        let queue = try fixture.queue(transport: fixture.transport())
        await queue.enqueue(negotiation.needed, filesIn: fixture.directory)
        let report = await queue.run()

        #expect(report.needsRenegotiation == [declarations[0].hash])
        #expect(fixture.fake.putCount == 1)  // one attempt, not three
    }

    /// The signature covers `Content-Length`, which `URLSession` takes from the body. So a file
    /// that no longer matches its ticket cannot produce a valid PUT — and must not be sent, because
    /// the store would then hold bytes its key does not describe.
    @Test func refusesToSendAFileWhoseLengthNoLongerMatchesItsTicket() async throws {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 100)
        let declarations = try fixture.declarations()
        let negotiation = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: declarations)

        // The file changes after the ticket was issued.
        try Data(repeating: 7, count: 101).write(to: fixture.directory.appending(path: "a.jpg"))

        let queue = try fixture.queue(transport: fixture.transport())
        await queue.enqueue(negotiation.needed, filesIn: fixture.directory)
        let report = await queue.run()

        #expect(report.failed.count == 1)
        #expect(fixture.fake.putCount == 0)
    }

    @Test func refusesToSendAFileThatIsGone() async throws {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 100)
        let declarations = try fixture.declarations()
        let negotiation = try await fixture.negotiateClient.negotiate(
            tripID: "trip-1", assets: declarations)
        try FileManager.default.removeItem(at: fixture.directory.appending(path: "a.jpg"))

        let queue = try fixture.queue(transport: fixture.transport())
        await queue.enqueue(negotiation.needed, filesIn: fixture.directory)
        let report = await queue.run()

        #expect(report.failed.count == 1)
        #expect(fixture.fake.putCount == 0)
    }

    /// An expired ticket is never sent. Its control is every other test here, where a ticket
    /// expiring in 2099 is sent.
    @Test func doesNotSendAnExpiredTicket() async throws {
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        try fixture.write("a.jpg", bytes: 8)
        let declarations = try fixture.declarations()
        let expired = UploadTicket(
            hash: declarations[0].hash, filename: "a.jpg", storedFilename: "a.jpg",
            filenameAdjusted: false, size: 8, method: "PUT",
            putURL: URL(string: "https://\(fixture.fake.host)/store/\(declarations[0].hash)")!,
            headers: [:], expiresAtRaw: "2020-01-01T00:00:00+00:00",
            expiresAt: ServiceTime.date(from: "2020-01-01T00:00:00+00:00"),
            replacesMismatchedObject: false)

        let queue = try fixture.queue(transport: fixture.transport())
        await queue.enqueue([expired], filesIn: fixture.directory)
        let report = await queue.run()

        #expect(report.needsRenegotiation == [declarations[0].hash])
        #expect(fixture.fake.putCount == 0)
    }
}

struct BackgroundSessionShapeTests {
    /// The background configuration is what the app must use, and getting these two flags wrong is
    /// invisible until a traveller's upload never starts: a discretionary upload is deferred to a
    /// time the system chooses, and without launch events the completions of an app the system
    /// killed are dropped.
    @Test func theBackgroundConfigurationIsNotDiscretionaryAndSendsLaunchEvents() {
        let configuration = URLSessionUploadTransport.backgroundConfiguration(
            identifier: "test.upload")
        #expect(configuration.isDiscretionary == false)
        #expect(configuration.sessionSendsLaunchEvents == true)
        #expect(configuration.identifier == "test.upload")
    }

    /// A transport that cannot survive a process restart says so by adopting nothing, rather than
    /// by claiming it re-attached to work it has no record of.
    @Test func aTransportWithNoOutstandingTasksAdoptsNothing() async throws {
        struct Nowhere: UploadTransport {
            func send(_ request: URLRequest, fromFile file: URL, named taskName: String)
                async throws -> UploadOutcome
            {
                UploadOutcome(statusCode: 200, bytesSent: 0)
            }
        }
        let fixture = try UploadFixture()
        defer { fixture.tearDown() }
        let queue = try fixture.queue(transport: Nowhere())
        #expect(await queue.adoptOutstanding().isEmpty)
    }
}
