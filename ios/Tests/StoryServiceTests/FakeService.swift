import Foundation

@testable import StoryService

/// A stand-in for `storybook-service` **and** for the object store behind it, answering the shapes
/// the real one answers (`service/storybook_service/ingest.py`, `jobs.py`).
///
/// It exists for one thing the real service cannot give a unit test: a *second* upload of an
/// unchanged trip, measured. So it models the only state that decides that — which hashes the store
/// holds, at what length — and counts the bytes it was actually PUT.
///
/// It is a fake, and it is not evidence about the real service: the live-loop driver in
/// `LiveLoopTests` is. What it *is* good for is the pair of runs, because a real bucket does not
/// exist yet.
final class FakeService: @unchecked Sendable {
    private let lock = NSLock()

    /// The host this fake answers on, unique per test. The store's presigned URLs point at the same
    /// host under `/store/`, which is a simplification with a real consequence made explicit: the
    /// *real* `put_url` is on S3 and carries no identity header, and `respond(to:)` enforces that
    /// distinction by path.
    let host: String

    init(host: String = StubHTTP.uniqueHost()) {
        self.host = host
    }

    /// Register with the stub and hand back the client trio pointed at it.
    func install() -> (endpoint: ServiceEndpoint, session: URLSession, identity: any ServiceIdentity) {
        StubHTTP.register(host: host) { [self] call in respond(to: call) }
        return (
            ServiceEndpoint(baseURL: URL(string: "https://\(host)")!),
            StubHTTP.session(),
            UnverifiedIdentityHeader(identity: "traveller@example.com")
        )
    }

    func uninstall() {
        StubHTTP.unregister(host: host)
    }

    /// hash -> length of the object the store holds.
    private var stored: [String: Int] = [:]
    /// hash -> length the client declared for it.
    private var declared: [String: (filename: String, size: Int)] = [:]
    private var putBytes: [String: Int] = [:]
    private var tripID = "trip-1"
    private var jobPolls = 0

    /// Statuses returned by `GET /jobs/{id}`, one per poll, last one repeating.
    var jobScript: [[String: Any]] = []
    /// Status codes to answer PUTs with, by hash. Anything absent gets 200.
    var putStatus: [String: Int] = [:]
    /// Hashes whose PUT should fail as a transport error, once each.
    var failPutOnce: Set<String> = []

    var bytesPut: Int {
        lock.lock()
        defer { lock.unlock() }
        return putBytes.values.reduce(0, +)
    }

    var putCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return putBytes.count
    }

    func bytesPut(for hash: String) -> Int {
        lock.lock()
        defer { lock.unlock() }
        return putBytes[hash] ?? 0
    }

    /// Pretend an earlier upload already landed, without going through a PUT.
    func seedStore(hash: String, size: Int) {
        lock.lock()
        stored[hash] = size
        lock.unlock()
    }

    func respond(to call: StubHTTP.Call) -> StubHTTP.Reply {
        let path = call.url.path
        // Every route on the real service answers 401 without this header, and no route has an
        // anonymous mode. A client that forgets it should fail here too, not silently work.
        if !path.hasPrefix("/store/"), call.headers["X-Story-Identity"] == nil {
            return .json(["detail": "no caller identity"], status: 401)
        }

        switch (call.method, path) {
        case ("POST", "/trips"):
            return .json(
                ["trip_id": tripID, "name": "Trip", "created_at": "2026-08-10T09:00:00.123456+00:00"],
                status: 201)

        case ("POST", "/trips/\(tripID)/assets:negotiate"):
            return negotiate(call)

        case ("POST", "/trips/\(tripID)/source:prepare"):
            return prepare()

        case ("POST", "/trips/\(tripID)/build"):
            return .json(job(state: "queued"), status: 202)

        case ("PUT", let p) where p.hasPrefix("/store/"):
            return put(hash: String(p.dropFirst("/store/".count)), call: call)

        case ("GET", let p) where p.hasPrefix("/jobs/"):
            return scriptedJob()

        default:
            return .json(["detail": "no route for \(call.method) \(path)"], status: 404)
        }
    }

    // MARK: - Routes

    private func negotiate(_ call: StubHTTP.Call) -> StubHTTP.Reply {
        struct Body: Decodable {
            struct Asset: Decodable {
                let hash: String
                let filename: String
                let size: Int
            }
            let assets: [Asset]
        }
        guard let body = try? JSONDecoder().decode(Body.self, from: call.body) else {
            return .json(["detail": "unparsable negotiate body"], status: 422)
        }
        for asset in body.assets where asset.hash.count != 128 {
            // The real service rejects a prefix with 422 rather than matching nothing.
            return .json(["detail": "hash must be 128 hex characters"], status: 422)
        }

        lock.lock()
        defer { lock.unlock() }
        var needed: [[String: Any]] = []
        var have: [[String: Any]] = []
        for asset in body.assets {
            declared[asset.hash] = (asset.filename, asset.size)
            if let held = stored[asset.hash], held == asset.size {
                have.append(["hash": asset.hash, "size": held])
                continue
            }
            needed.append([
                "hash": asset.hash,
                "filename": asset.filename,
                "stored_filename": asset.filename,
                "filename_adjusted": false,
                "size": asset.size,
                "method": "PUT",
                "put_url": "https://\(host)/store/\(asset.hash)?X-Amz-Signature=stub",
                "headers": ["Content-Length": String(asset.size)],
                "expires_at": "2099-01-01T00:00:00+00:00",
                "replaces_mismatched_object": stored[asset.hash] != nil,
            ])
        }
        return .json([
            "trip_id": tripID,
            "needed": needed,
            "have": have,
            "upload": [
                "multipart": false,
                "multipart_detail": "a single presigned PUT carries 5 GB",
                "presence_not_verified": "the service never reads the bytes",
            ],
        ])
    }

    private func put(hash: String, call: StubHTTP.Call) -> StubHTTP.Reply {
        lock.lock()
        if failPutOnce.contains(hash) {
            failPutOnce.remove(hash)
            lock.unlock()
            return StubHTTP.Reply(status: 503, body: Data("<Error>slow down</Error>".utf8))
        }
        let status = putStatus[hash] ?? 200
        if status == 200 {
            stored[hash] = call.body.count
        }
        putBytes[hash, default: 0] += call.body.count
        lock.unlock()
        if status == 200 { return StubHTTP.Reply(status: 200, body: Data(), headers: [:]) }
        return StubHTTP.Reply(status: status, body: Data("<Error>refused</Error>".utf8))
    }

    private func prepare() -> StubHTTP.Reply {
        lock.lock()
        defer { lock.unlock() }
        let missing = declared.filter { stored[$0.key] != $0.value.size }.map(\.key).sorted()
        return .json([
            "trip_id": tripID,
            "declared": declared.count,
            "fetched": declared.count - missing.count,
            "already_present": 0,
            "removed": [],
            "missing": missing,
            "complete": missing.isEmpty,
            "scaffolded": missing.isEmpty,
        ])
    }

    private func job(state: String) -> [String: Any] {
        [
            "job_id": "job-1", "trip_id": tripID, "kind": "build", "state": state,
            "stage": NSNull(), "done": 0, "total": NSNull(),
            "stage_index": NSNull(), "stages_total": NSNull(),
            "attempts": 0, "created_at": "2026-08-10T09:00:00+00:00",
            "queued_ahead": 0,
            "progress_basis": "queued: no work has started, so there is nothing measured",
        ]
    }

    private func scriptedJob() -> StubHTTP.Reply {
        lock.lock()
        defer { lock.unlock() }
        guard !jobScript.isEmpty else { return .json(job(state: "queued")) }
        let index = min(jobPolls, jobScript.count - 1)
        jobPolls += 1
        return .json(jobScript[index])
    }
}
