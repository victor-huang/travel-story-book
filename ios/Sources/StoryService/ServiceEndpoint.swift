import Foundation

/// Where the service is, how a request says who is calling, and how a failure reads.
///
/// Owned by I20–I22. `Auth.swift` (I23) replaces exactly one type here — ``ServiceIdentity`` —
/// and nothing else in this target needs to change when it does.

// MARK: - Endpoint

public struct ServiceEndpoint: Sendable, Equatable {
    public let baseURL: URL

    public init(baseURL: URL) {
        self.baseURL = baseURL
    }

    /// A URL for one service path.
    ///
    /// Built by string concatenation rather than `appending(path:)` because two of the routes have
    /// a **colon inside a path segment** (`assets:negotiate`, `source:prepare`). A percent-encoded
    /// colon is a different path, and the service answers 404 for it.
    public func url(_ path: String) -> URL {
        let base = baseURL.absoluteString.hasSuffix("/")
            ? String(baseURL.absoluteString.dropLast())
            : baseURL.absoluteString
        let tail = path.hasPrefix("/") ? path : "/" + path
        guard let url = URL(string: base + tail) else {
            // Only reachable if `baseURL` was already not a URL, which the type forbids.
            preconditionFailure("could not form a URL from \(base) + \(tail)")
        }
        return url
    }
}

// MARK: - Identity (the seam S06 replaces)

/// Who the caller is, as headers on every request.
///
/// This is a seam, not a feature: the service has **no authentication yet** (S06), and the only
/// implementation here says so in its name.
public protocol ServiceIdentity: Sendable {
    func identityHeaders() async throws -> [String: String]
}

/// **A stub. Not a design.**
///
/// The service reads `X-Story-Identity: <email|phone>` and *believes it without verification* —
/// its own `principal.py` logs a warning to that effect at every start, and answers 401 only when
/// the header is absent. So this type transmits an unverified claim, which is fine against
/// localhost and is a hole anywhere else.
///
/// S06 replaces it with verified Google (then Apple) sign-in. When that lands, this type should be
/// **deleted**, not kept as a fallback: a fallback to an unverified identity is how a deployment
/// ends up trusting anyone who asks.
public struct UnverifiedIdentityHeader: ServiceIdentity {
    public static let headerName = "X-Story-Identity"

    /// An email address or a phone number, per the service's question 17.
    public let identity: String

    public init(identity: String) {
        self.identity = identity
    }

    public func identityHeaders() async throws -> [String: String] {
        [Self.headerName: identity]
    }
}

// MARK: - Errors

public enum ServiceError: Error, Sendable, Equatable {
    /// 401. Today that means the identity header was missing; after S06 it means it was rejected.
    case unauthorized(String)
    /// 404. The service answers this for "not yours" as well as "not there", deliberately, so a
    /// caller cannot enumerate trip or job ids. Do not report it to a user as "deleted".
    case notFound(String)
    /// Any other non-2xx, with the service's own `detail` where it sent one.
    case rejected(status: Int, detail: String)
    /// 2xx whose body was not the shape this client decodes.
    case malformedResponse(String)
    case transport(String)

    public var isRetryable: Bool {
        switch self {
        case .transport: true
        case .rejected(let status, _): status == 429 || (500...599).contains(status)
        case .unauthorized, .notFound, .malformedResponse: false
        }
    }
}

// MARK: - Timestamps

/// The service's timestamps are `datetime.isoformat()`: an offset, and six fractional digits when
/// the clock had them. `ISO8601DateFormatter` is fussy about the fraction, so it is normalised to
/// three digits before parsing rather than hoped over.
///
/// Every parse returns an optional and **no caller substitutes a default**. An unparsed expiry
/// reads as *unknown*, never as "not expired".
public enum ServiceTime {
    public static func date(from raw: String) -> Date? {
        let normalised = normaliseFraction(raw)
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFraction.date(from: normalised) { return date }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: normalised)
    }

    /// `12:00:00.123456+00:00` → `12:00:00.123+00:00`; a value with no fraction is untouched.
    static func normaliseFraction(_ raw: String) -> String {
        guard let dot = raw.firstIndex(of: ".") else { return raw }
        var digitsEnd = raw.index(after: dot)
        while digitsEnd < raw.endIndex, raw[digitsEnd].isNumber {
            digitsEnd = raw.index(after: digitsEnd)
        }
        let digits = raw[raw.index(after: dot)..<digitsEnd]
        guard digits.count > 3 else { return raw }
        return String(raw[raw.startIndex...dot]) + digits.prefix(3) + String(raw[digitsEnd...])
    }
}

// MARK: - The HTTP core

/// One place that adds the identity headers, checks the status and decodes the body.
///
/// It is `internal` on purpose: the three public clients are the API, and a caller that can send
/// arbitrary paths would make the wire contract untestable.
struct ServiceHTTP: Sendable {
    let endpoint: ServiceEndpoint
    let identity: any ServiceIdentity
    let session: URLSession

    /// An empty JSON object. `POST /trips/{id}/build` takes no body fields, deliberately (the
    /// config a build uses is the one `story-book init` *measured*, not one the phone cached), and
    /// `source:prepare` takes none at all — but both are POSTs, so they send `{}`.
    struct Empty: Codable, Sendable {}

    func get<Response: Decodable>(_ path: String, as type: Response.Type) async throws -> Response {
        try await send(path, method: "GET", body: nil as Empty?, as: type)
    }

    func post<Response: Decodable>(_ path: String, as type: Response.Type) async throws -> Response {
        try await send(path, method: "POST", body: Empty(), as: type)
    }

    func post<Body: Encodable, Response: Decodable>(
        _ path: String, body: Body, as type: Response.Type
    ) async throws -> Response {
        try await send(path, method: "POST", body: body, as: type)
    }

    private func send<Body: Encodable, Response: Decodable>(
        _ path: String, method: String, body: Body?, as type: Response.Type
    ) async throws -> Response {
        let url = endpoint.url(path)
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        for (name, value) in try await identity.identityHeaders() {
            request.setValue(value, forHTTPHeaderField: name)
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(body)
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw ServiceError.transport("\(method) \(url.path): \(error)")
        }
        guard let http = response as? HTTPURLResponse else {
            throw ServiceError.malformedResponse("not an HTTP response from \(url)")
        }
        guard (200...299).contains(http.statusCode) else {
            let detail = Self.detail(in: data)
            switch http.statusCode {
            case 401: throw ServiceError.unauthorized(detail)
            case 404: throw ServiceError.notFound(detail)
            default: throw ServiceError.rejected(status: http.statusCode, detail: detail)
            }
        }
        do {
            return try JSONDecoder().decode(Response.self, from: data)
        } catch {
            throw ServiceError.malformedResponse("\(url.path): \(error)")
        }
    }

    /// FastAPI puts its own words in `detail`; a validation error puts a list there. Both are
    /// carried through rather than paraphrased — the service's message names the field.
    static func detail(in data: Data) -> String {
        guard
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let detail = object["detail"]
        else {
            return String(data: data.prefix(2000), encoding: .utf8) ?? "<no body>"
        }
        if let text = detail as? String { return text }
        return String(describing: detail)
    }
}

// MARK: - Composition

/// The three clients over one endpoint and one identity, so the app configures the service once.
public struct StoryServiceClient: Sendable {
    public let negotiate: NegotiateClient
    public let jobs: JobPoller

    public init(
        endpoint: ServiceEndpoint,
        identity: any ServiceIdentity,
        session: URLSession = .shared
    ) {
        self.negotiate = NegotiateClient(endpoint: endpoint, identity: identity, session: session)
        self.jobs = JobPoller(endpoint: endpoint, identity: identity, session: session)
    }
}
