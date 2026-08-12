import Foundation

@testable import StoryService

/// A minimal stand-in for `POST /trips/{id}/assets:negotiate` and the presigned `PUT`, scoped to
/// `StoryAppTests` — `StoryServiceTests`' own `FakeService`/`StubHTTP` are internal to that other
/// test target and are not visible here. Deliberately smaller than that fake: `MusicImporter`
/// only ever negotiates and uploads one asset at a time, so this does not need batching, `have`,
/// or retry-classification coverage — `StoryServiceTests` already owns proving those.
final class MusicStubHTTP: URLProtocol, @unchecked Sendable {
    struct Reply: Sendable {
        var status: Int = 200
        var body: Data = Data()
    }
    struct Call: Sendable {
        let method: String
        let url: URL
        let body: Data
    }

    private static let lock = NSLock()
    nonisolated(unsafe) private static var handlers: [String: @Sendable (Call) -> Reply] = [:]

    static func register(host: String, handler: @escaping @Sendable (Call) -> Reply) {
        lock.lock()
        handlers[host] = handler
        lock.unlock()
    }

    static func unregister(host: String) {
        lock.lock()
        handlers[host] = nil
        lock.unlock()
    }

    static func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MusicStubHTTP.self]
        return URLSession(configuration: configuration)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url, let host = url.host() else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        let call = Call(
            method: request.httpMethod ?? "GET", url: url, body: Self.readBody(of: request))
        Self.lock.lock()
        let handler = Self.handlers[host]
        Self.lock.unlock()
        guard let handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotFindHost))
            return
        }
        let reply = handler(call)
        let response = HTTPURLResponse(
            url: url, statusCode: reply.status, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if !reply.body.isEmpty { client?.urlProtocol(self, didLoad: reply.body) }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func readBody(of request: URLRequest) -> Data {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: buffer.count)
            if read <= 0 { break }
            data.append(contentsOf: buffer[0..<read])
        }
        return data
    }
}

/// Answers exactly what `MusicImporter` needs: one negotiate, one PUT, both counted so a test can
/// assert *how many* network calls happened (zero, for a refused track).
final class MusicFakeService: @unchecked Sendable {
    let host = "music-fake-\(UUID().uuidString.prefix(8).lowercased()).invalid"
    private let lock = NSLock()
    private(set) var negotiateCallCount = 0
    private(set) var putCallCount = 0

    func install() -> (endpoint: ServiceEndpoint, session: URLSession, identity: any ServiceIdentity)
    {
        MusicStubHTTP.register(host: host) { [self] call in respond(to: call) }
        return (
            ServiceEndpoint(baseURL: URL(string: "https://\(host)")!),
            MusicStubHTTP.session(),
            UnverifiedIdentityHeader(identity: "traveller@example.com")
        )
    }

    func uninstall() {
        MusicStubHTTP.unregister(host: host)
    }

    func uploadTransport() -> URLSessionUploadTransport {
        URLSessionUploadTransport(configuration: MusicStubHTTP.session().configuration)
    }

    private func respond(to call: MusicStubHTTP.Call) -> MusicStubHTTP.Reply {
        lock.lock()
        defer { lock.unlock() }

        if call.url.path.contains("assets:negotiate") {
            negotiateCallCount += 1
            let object = try? JSONSerialization.jsonObject(with: call.body) as? [String: Any]
            let assets = (object?["assets"] as? [[String: Any]]) ?? []
            let needed = assets.map { asset -> [String: Any] in
                let hash = asset["hash"] as? String ?? ""
                let filename = asset["filename"] as? String ?? ""
                let size = asset["size"] as? Int ?? 0
                return [
                    "hash": hash,
                    "filename": filename,
                    "stored_filename": filename,
                    "filename_adjusted": false,
                    "size": size,
                    "method": "PUT",
                    "put_url": "https://\(host)/store/\(hash)",
                    "headers": [String: String](),
                    "expires_at": ISO8601DateFormatter().string(from: Date().addingTimeInterval(3600)),
                    "replaces_mismatched_object": false,
                ]
            }
            let body: [String: Any] = [
                "trip_id": "trip-1",
                "needed": needed,
                "have": [],
                "upload": ["multipart": false],
            ]
            return .init(
                status: 200, body: try! JSONSerialization.data(withJSONObject: body))
        }

        if call.url.path.hasPrefix("/store/") {
            putCallCount += 1
            return .init(status: 200, body: Data())
        }

        return .init(status: 404, body: Data())
    }
}
