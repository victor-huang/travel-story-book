import Foundation

/// A `URLProtocol` that answers from a closure, so every test in this target runs with **no
/// network at all**: no host, and above all no S3 bucket — one does not exist yet, and a test that
/// needed it would be a test nobody can run.
///
/// Handlers are registered **per host**, and every test invents its own host. A single static
/// handler would have been simpler and wrong: swift-testing runs suites in parallel, so two tests
/// would have overwritten each other's stub and the failure would have looked like a flaky client.
///
/// The closure sees the body, including an upload task's file body, so a test can measure what was
/// actually transferred rather than assume it from a file's length.
final class StubHTTP: URLProtocol, @unchecked Sendable {
    struct Reply: Sendable {
        var status: Int = 200
        var body: Data = Data()
        var headers: [String: String] = ["Content-Type": "application/json"]

        static func json(_ object: Any, status: Int = 200) -> Reply {
            Reply(
                status: status,
                body: try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]))
        }
    }

    struct Call: Sendable {
        let method: String
        let url: URL
        let headers: [String: String]
        let body: Data
    }

    private static let lock = NSLock()
    nonisolated(unsafe) private static var handlers: [String: @Sendable (Call) -> Reply] = [:]
    nonisolated(unsafe) private static var calls: [String: [Call]] = [:]

    /// A host nobody owns, unique per test. `.invalid` is reserved by RFC 2606, so a request that
    /// escaped the stub could not reach anything.
    static func uniqueHost() -> String {
        "t\(UUID().uuidString.prefix(8).lowercased()).invalid"
    }

    static func register(host: String, handler: @escaping @Sendable (Call) -> Reply) {
        lock.lock()
        handlers[host] = handler
        calls[host] = []
        lock.unlock()
    }

    static func unregister(host: String) {
        lock.lock()
        handlers[host] = nil
        calls[host] = nil
        lock.unlock()
    }

    static func recorded(host: String) -> [Call] {
        lock.lock()
        defer { lock.unlock() }
        return calls[host] ?? []
    }

    /// A session wired to this protocol. Ephemeral rather than background, because `URLProtocol`
    /// stubs are not consulted by a background session — the real background configuration is used
    /// by the app and is exercised only by the live-loop driver.
    static func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubHTTP.self]
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
            method: request.httpMethod ?? "GET",
            url: url,
            headers: request.allHTTPHeaderFields ?? [:],
            body: Self.readBody(of: request))

        Self.lock.lock()
        Self.calls[host, default: []].append(call)
        let handler = Self.handlers[host]
        Self.lock.unlock()

        guard let handler else {
            // Loud rather than empty: an unregistered host means the test asked for a host it never
            // stubbed, and a 500 with no body reads like a server bug.
            client?.urlProtocol(self, didFailWithError: URLError(.cannotFindHost))
            return
        }
        let reply = handler(call)
        let response = HTTPURLResponse(
            url: url, statusCode: reply.status, httpVersion: "HTTP/1.1",
            headerFields: reply.headers)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if !reply.body.isEmpty {
            client?.urlProtocol(self, didLoad: reply.body)
        }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func readBody(of request: URLRequest) -> Data {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var data = Data()
        let size = 64 * 1024
        var buffer = [UInt8](repeating: 0, count: size)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: size)
            if read <= 0 { break }
            data.append(contentsOf: buffer[0..<read])
        }
        return data
    }
}
