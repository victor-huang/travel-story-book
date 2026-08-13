import Foundation
import Testing

@testable import StoryApp
@testable import StoryService

/// `ReelClient` covers the two routes I30/I31/I33 needed that neither `NegotiateClient` nor
/// `JobPoller` already had: `POST /trips/{id}/reel` and `GET /jobs/{id}/reel`. It shipped with the
/// live curl round trip in the I30-33 log entry as its only evidence — real, but not something a
/// later change to this file re-runs. These are the client-side unit tests that were missing:
/// what gets sent, and how each documented status code (`422`, `404`, `409`) is told apart.
///
/// No network: `StubURLProtocol` answers from a closure per host, the same shape
/// `StoryServiceTests/StubHTTP.swift` already uses — duplicated rather than imported, because
/// `Package.swift` (I01's file) declares no dependency from `StoryAppTests` onto
/// `StoryServiceTests`, the same call I18's `SelectionSeeding` already made for
/// `PhotoExportTests/LibraryFixtures.swift`.
private final class StubURLProtocol: URLProtocol, @unchecked Sendable {
    struct Reply {
        var status: Int = 200
        var body: Data = Data()
    }

    private static let lock = NSLock()
    nonisolated(unsafe) private static var handlers: [String: @Sendable (URLRequest, Data?) -> Reply] = [:]

    static func register(host: String, handler: @escaping @Sendable (URLRequest, Data?) -> Reply) {
        lock.lock()
        handlers[host] = handler
        lock.unlock()
    }

    static func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url, let host = url.host() else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        Self.lock.lock()
        let handler = Self.handlers[host]
        Self.lock.unlock()
        guard let handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        let reply = handler(request, bodyData())
        let response = HTTPURLResponse(
            url: url, statusCode: reply.status, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: reply.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private func bodyData() -> Data? {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        let bufferSize = 4096
        var buffer = [UInt8](repeating: 0, count: bufferSize)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: bufferSize)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return data
    }
}

/// A locked box for what a `StubURLProtocol` handler captures — the handler runs on
/// `URLSession`'s own queue, not the test's, so a plain `var` capture is a data race under Swift
/// 6 strict concurrency, not just a style complaint.
private final class Captured<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value

    init(_ initial: Value) { value = initial }

    func set(_ newValue: Value) {
        lock.lock()
        value = newValue
        lock.unlock()
    }

    func get() -> Value {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

@Suite struct ReelClientTests {
    private func makeClient(host: String) -> ReelClient {
        ReelClient(
            endpoint: ServiceEndpoint(baseURL: URL(string: "https://\(host)")!),
            identity: UnverifiedIdentityHeader(identity: "traveller@example.com"),
            session: StubURLProtocol.session())
    }

    private func uniqueHost() -> String {
        "reel-\(UUID().uuidString.prefix(8).lowercased()).invalid"
    }

    // MARK: startReel

    @Test func startRealSendsEveryOptionOverTheWireExactlyAsSet() async throws {
        let host = uniqueHost()
        let capturedBody = Captured<[String: Any]?>(nil)
        let capturedHeaders = Captured<[String: String]>([:])
        StubURLProtocol.register(host: host) { request, body in
            capturedHeaders.set(request.allHTTPHeaderFields ?? [:])
            if let body, let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
                capturedBody.set(object)
            }
            return .init(
                status: 202,
                body: try! JSONSerialization.data(withJSONObject: [
                    "job_id": "job-1", "trip_id": "trip-1", "state": "queued", "created": true,
                ]))
        }

        var options = ReelOptions()
        options.aspect = "9:16"
        options.musicHash = "abc123"
        options.dateFrom = "2026-01-01"
        options.dateTo = "2026-01-03"
        options.places = ["Kyoto", "Osaka"]
        options.name = "Spring trip"
        options.subtitles = ["en"]
        options.burnIn = "en"
        options.clipAudio = false

        let start = try await makeClient(host: host).startReel(tripID: "trip-1", options: options)

        #expect(start.jobID == "job-1")
        #expect(start.tripID == "trip-1")
        #expect(start.created == true)
        #expect(capturedHeaders.get()[UnverifiedIdentityHeader.headerName] == "traveller@example.com")

        let body = try #require(capturedBody.get())
        #expect(body["aspect"] as? String == "9:16")
        #expect(body["music_hash"] as? String == "abc123")
        #expect(body["date_from"] as? String == "2026-01-01")
        #expect(body["date_to"] as? String == "2026-01-03")
        #expect(body["places"] as? [String] == ["Kyoto", "Osaka"])
        #expect(body["name"] as? String == "Spring trip")
        #expect(body["subtitles"] as? [String] == ["en"])
        #expect(body["burn_in"] as? String == "en")
        #expect(body["clip_audio"] as? Bool == false)
    }

    /// The control for the test above: an option genuinely left unset must arrive as JSON `null`,
    /// never silently defaulted to something the caller never asked for (`ReelOptions`'s own
    /// doc comment: "there is no client-side default that silently overrides what a screen left
    /// unset").
    @Test func anUnsetOptionArrivesAsExplicitNullNotAsAnAbsentKey() async throws {
        let host = uniqueHost()
        let capturedBody = Captured<[String: Any]?>(nil)
        StubURLProtocol.register(host: host) { _, body in
            if let body, let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
                capturedBody.set(object)
            }
            return .init(
                status: 202,
                body: try! JSONSerialization.data(withJSONObject: [
                    "job_id": "job-2", "trip_id": "trip-1", "state": "queued", "created": true,
                ]))
        }

        _ = try await makeClient(host: host).startReel(tripID: "trip-1", options: ReelOptions())

        let body = try #require(capturedBody.get())
        #expect(body.keys.contains("music_hash"))
        #expect(body["music_hash"] is NSNull)
    }

    @Test func aRejectedOptionIs422BeforeAnyJobIsQueued() async throws {
        let host = uniqueHost()
        StubURLProtocol.register(host: host) { _, _ in
            .init(
                status: 422,
                body: try! JSONSerialization.data(withJSONObject: ["detail": "bad aspect"]))
        }

        await #expect(throws: ServiceError.rejected(status: 422, detail: "bad aspect")) {
            _ = try await makeClient(host: host).startReel(tripID: "trip-1", options: ReelOptions())
        }
    }

    // MARK: reelDownload

    @Test func reelDownloadParsesTheFullShapeIncludingTheInlineManifest() async throws {
        let host = uniqueHost()
        StubURLProtocol.register(host: host) { _, _ in
            .init(
                status: 200,
                body: try! JSONSerialization.data(withJSONObject: [
                    "job_id": "job-1", "trip_id": "trip-1", "state": "succeeded",
                    "video": [
                        "download_url": "https://\(host)/video.mp4",
                        "expires_at": "2026-01-01T00:00:00+00:00",
                        "size_bytes": 4_200_000,
                    ],
                    "immutable": true,
                    "reel_json": ["video": ["aspect": "9:16"], "audio": ["music_supplied": true]],
                ]))
        }

        let download = try await makeClient(host: host).reelDownload(jobID: "job-1")

        #expect(download.jobID == "job-1")
        #expect(download.tripID == "trip-1")
        #expect(download.videoSizeBytes == 4_200_000)
        #expect(download.immutable == true)
        #expect(download.videoDownloadURL.absoluteString == "https://\(host)/video.mp4")
        #expect(download.manifest.value(at: "video.aspect") as? String == "9:16")
        #expect(download.manifest.value(at: "audio.music_supplied") as? Bool == true)
    }

    /// A build's job id, not a reel's — the service's documented 404. Distinct from the 409
    /// below: one means "wrong kind of job entirely", the other "right kind, not ready yet",
    /// and `ReelViewerScreen` reads these two differently.
    @Test func aBuildJobIDIs404NotAnUnreadyReel() async throws {
        let host = uniqueHost()
        StubURLProtocol.register(host: host) { _, _ in
            .init(
                status: 404,
                body: try! JSONSerialization.data(withJSONObject: ["detail": "not a reel job"]))
        }

        await #expect(throws: ServiceError.notFound("not a reel job")) {
            _ = try await makeClient(host: host).reelDownload(jobID: "build-job")
        }
    }

    /// The control for the 404 case above: a reel still queued or rendering is a *different*
    /// status (409), and a viewer must be able to tell "wrong kind of job" apart from "right
    /// kind, keep waiting" rather than treating every non-2xx the same way.
    @Test func aStillRenderingReelIs409NotA404() async throws {
        let host = uniqueHost()
        StubURLProtocol.register(host: host) { _, _ in
            .init(
                status: 409,
                body: try! JSONSerialization.data(withJSONObject: ["detail": "still rendering"]))
        }

        await #expect(throws: ServiceError.rejected(status: 409, detail: "still rendering")) {
            _ = try await makeClient(host: host).reelDownload(jobID: "job-1")
        }
    }
}
