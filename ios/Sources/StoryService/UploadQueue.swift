import Foundation

/// I21. Move the bytes: one presigned `PUT` per asset, **straight to object storage**, so the API
/// server never proxies 600 MB. Per-file retry, and a relaunch does not re-send what finished.
///
/// The acceptance criterion is *killing the app mid-upload and relaunching resumes without
/// re-sending completed assets*, and two independent mechanisms deliver it:
///
/// 1. **The service's `have`.** Media is keyed by content hash, so a second negotiate asks for
///    nothing it already holds. This is the one that works after a reinstall.
/// 2. **This queue's own durable state**, committed after **every** asset — the same rule
///    `ExportLedger` follows, and for the same reason: an interrupt must cost at most one item.
///    This is the one that works before a negotiate, or with the network down.
///
/// **No multipart.** S02 decided against it and says `multipart: false` in the negotiate response
/// with its reasoning: one presigned `PUT` carries 5 GB and a 1080p export is tens of MB, so
/// multipart buys resume *within* one file, not capability.
///
/// ### What a background `URLSession` actually constrains
///
/// A background session keeps uploading after the app is suspended or killed, and that has two
/// consequences this design is built around rather than discovering:
///
/// - **It will not carry a closure across a process restart.** So no completion handler is the
///   record of anything. The record is the state file; a restored task is identified by its
///   `taskDescription`, which *is* preserved, and set to the asset's content hash.
/// - **It needs a delegate that exists before the tasks are enumerated.** So the transport creates
///   the session and owns the delegate, and `adoptOutstanding()` re-attaches to whatever the last
///   process left running instead of starting it again.
///
/// A presigned URL is *not* durable state — it expires (an hour by default). After a relaunch the
/// app negotiates again, which is cheap and transfers zero bytes for everything already there, and
/// enqueues the fresh tickets.

// MARK: - Transport

public struct UploadOutcome: Sendable, Equatable {
    public let statusCode: Int
    /// Bytes this process actually put on the wire, or **`nil` when the transport could not
    /// measure it**.
    ///
    /// It is read from the task and never inferred from the file's size, and that distinction is
    /// not theoretical: a `URLSession` load served by a custom `URLProtocol` reports
    /// `countOfBytesSent == 0` and calls no `didSendBodyData`, so the first version of this type
    /// reported **0 bytes transferred for an upload that moved 350** — a zero that would have made
    /// "a second upload transfers zero bytes" pass on the first upload too. A measurement that can
    /// come back empty must say so rather than agree with itself.
    public let bytesSent: Int?
    /// The store's error body when it sent one. S3 answers XML; carrying it beats paraphrasing.
    public let body: String?

    public init(statusCode: Int, bytesSent: Int?, body: String? = nil) {
        self.statusCode = statusCode
        self.bytesSent = bytesSent
        self.body = body
    }
}

public protocol UploadTransport: Sendable {
    func send(_ request: URLRequest, fromFile file: URL, named taskName: String) async throws
        -> UploadOutcome
    /// Task names a previous process left in flight in this session, if the transport can survive
    /// one. An empty array is the honest answer for a transport that cannot.
    func adoptOutstanding() async -> [String]
    /// Await a task adopted from a previous process, by name.
    func awaitOutcome(named taskName: String) async throws -> UploadOutcome
}

extension UploadTransport {
    public func adoptOutstanding() async -> [String] { [] }
    public func awaitOutcome(named taskName: String) async throws -> UploadOutcome {
        throw ServiceError.transport("this transport cannot adopt task \(taskName)")
    }
}

/// A `URLSession` upload transport. Configure it with
/// ``backgroundConfiguration(identifier:)`` in the app; tests pass an ephemeral configuration with
/// a stubbed `URLProtocol`, which exercises this same delegate path with no network.
public final class URLSessionUploadTransport: NSObject, UploadTransport, URLSessionDataDelegate,
    @unchecked Sendable
{
    /// Background so the upload survives the app being suspended, and `isDiscretionary = false`
    /// because the traveller is watching a progress view — discretionary uploads are deferred to
    /// "a good time" that may be hours away.
    ///
    /// `sessionSendsLaunchEvents` means the system relaunches the app to hand over completions.
    /// The app must forward `application(_:handleEventsForBackgroundURLSession:completionHandler:)`
    /// and construct a transport with this identifier before that handler returns, or the events
    /// are dropped.
    public static func backgroundConfiguration(identifier: String) -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.background(withIdentifier: identifier)
        configuration.isDiscretionary = false
        configuration.sessionSendsLaunchEvents = true
        return configuration
    }

    private let lock = NSLock()
    private var sent: [String: Int] = [:]
    private var waiters: [String: CheckedContinuation<UploadOutcome, any Error>] = [:]
    /// Outcomes that arrived before anyone awaited them. Without this a completion delivered
    /// between `resume()` and the `await` would be lost, which is a hang rather than an error.
    private var settled: [String: Result<UploadOutcome, any Error>] = [:]
    private var bodies: [String: Data] = [:]
    private var session: URLSession!

    public init(configuration: URLSessionConfiguration) {
        super.init()
        self.session = URLSession(
            configuration: configuration, delegate: self, delegateQueue: nil)
    }

    public func send(_ request: URLRequest, fromFile file: URL, named taskName: String) async throws
        -> UploadOutcome
    {
        let task = session.uploadTask(with: request, fromFile: file)
        // The only piece of per-task state that survives a process restart.
        task.taskDescription = taskName
        task.resume()
        return try await awaitOutcome(named: taskName)
    }

    public func adoptOutstanding() async -> [String] {
        await session.allTasks.compactMap(\.taskDescription)
    }

    public func awaitOutcome(named taskName: String) async throws -> UploadOutcome {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            if let already = settled.removeValue(forKey: taskName) {
                lock.unlock()
                continuation.resume(with: already)
                return
            }
            waiters[taskName] = continuation
            lock.unlock()
        }
    }

    public func finishTasksAndInvalidate() {
        session.finishTasksAndInvalidate()
    }

    // MARK: URLSessionDataDelegate

    public func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data)
    {
        guard let name = dataTask.taskDescription else { return }
        lock.lock()
        bodies[name, default: Data()].append(data)
        lock.unlock()
    }

    public func urlSession(
        _ session: URLSession, task: URLSessionTask, totalBytesSent: Int64,
        didSendBodyData bytesSent: Int64, totalBytesExpectedToSend: Int64
    ) {
        guard let name = task.taskDescription else { return }
        lock.lock()
        sent[name] = Int(totalBytesSent)
        lock.unlock()
    }

    public func urlSession(
        _ session: URLSession, task: URLSessionTask, didCompleteWithError error: (any Error)?
    ) {
        guard let name = task.taskDescription else { return }
        lock.lock()
        let body = bodies.removeValue(forKey: name)
        let reported = max(sent.removeValue(forKey: name) ?? 0, Int(task.countOfBytesSent))
        let result: Result<UploadOutcome, any Error>
        if let error {
            result = .failure(ServiceError.transport("PUT \(name): \(error)"))
        } else if let http = task.response as? HTTPURLResponse {
            result = .success(
                UploadOutcome(
                    statusCode: http.statusCode,
                    // Zero means "nobody counted": a custom URLProtocol reports no progress at
                    // all, and an empty file is not a case this queue has. Reported as unmeasured
                    // rather than as a zero that would look like a measurement.
                    bytesSent: reported > 0 ? reported : nil,
                    body: body.flatMap { String(data: $0.prefix(2000), encoding: .utf8) }))
        } else {
            result = .failure(ServiceError.malformedResponse("no HTTP response for PUT \(name)"))
        }
        let waiter = waiters.removeValue(forKey: name)
        if waiter == nil { settled[name] = result }
        lock.unlock()
        waiter?.resume(with: result)
    }
}

// MARK: - The queue

public actor UploadQueue {
    public enum AssetState: String, Codable, Sendable {
        case pending
        case uploading
        case uploaded
        /// Terminal for this run. Retried on a later run — a failure is often transient, and the
        /// pipeline's own cache semantics make the same choice.
        case failed
        /// The grant is gone (expired, or the signature was refused). Retrying the same URL cannot
        /// help; the client must negotiate again. Named rather than folded into `failed`, because
        /// the two want different actions from the app.
        case needsRenegotiation
    }

    public struct Entry: Codable, Sendable, Equatable {
        public var hash: String
        public var filename: String
        public var size: Int
        public var state: AssetState
        public var attempts: Int
        /// Bytes sent for this asset, summed across attempts, or `nil` where the transport could
        /// not measure. Never the file's size standing in for a measurement.
        public var bytesSent: Int?
        public var lastError: String?
        public var completedAt: String?
    }

    public struct Report: Sendable, Equatable {
        public var uploaded: [String] = []
        /// Skipped because this queue's own durable state already records them as uploaded.
        public var alreadySentEarlier: [String] = []
        public var failed: [String: String] = [:]
        public var needsRenegotiation: [String] = []
        /// Assets this run put on the wire, counted whether or not their bytes could be measured.
        public var assetsSent: Int = 0
        var bytesMeasured: Int = 0
        var uploadsUnmeasured: Int = 0

        /// Bytes this run transferred: **`0` only when nothing was sent**, and `nil` when something
        /// was sent that the transport could not measure.
        ///
        /// The distinction is the whole of I20's criterion. A single `Int` here would have read `0`
        /// for an upload of 350 bytes under a stubbed `URLProtocol`, and "a second upload transfers
        /// zero bytes" would have been true of the first upload as well.
        public var bytesTransferred: Int? {
            uploadsUnmeasured == 0 ? bytesMeasured : nil
        }

        public var isComplete: Bool { failed.isEmpty && needsRenegotiation.isEmpty }
    }

    public enum QueueError: Error, Sendable, Equatable {
        case fileMissing(hash: String, path: String)
        /// The signature covers `Content-Length`, and `URLSession` computes that from the body. So
        /// a file whose length disagrees with the ticket cannot produce a valid PUT — and if it
        /// somehow did, the store would hold bytes its key does not describe.
        case lengthMismatch(hash: String, declared: Int, onDisk: Int)
    }

    private struct StateFile: Codable, Sendable {
        var schemaVersion: Int = 1
        var entries: [Entry]
    }

    private let stateURL: URL
    private let transport: any UploadTransport
    private let maxAttempts: Int
    private let retryDelay: Duration
    private var entries: [String: Entry]
    /// This launch's tickets. Not durable: a presigned URL expires, so it is never persisted and
    /// never re-used across a relaunch.
    private var tickets: [UploadTicket] = []
    private var fileDirectory: URL?

    public init(
        stateURL: URL,
        transport: any UploadTransport,
        maxAttempts: Int = 3,
        retryDelay: Duration = .seconds(2)
    ) throws {
        self.stateURL = stateURL
        self.transport = transport
        self.maxAttempts = maxAttempts
        self.retryDelay = retryDelay
        if let data = try? Data(contentsOf: stateURL) {
            let decoded = try JSONDecoder().decode(StateFile.self, from: data)
            self.entries = Dictionary(
                decoded.entries.map { ($0.hash, $0) }, uniquingKeysWith: { _, last in last })
        } else {
            self.entries = [:]
        }
    }

    public var state: [Entry] { entries.values.sorted { $0.hash < $1.hash } }

    public func entry(for hash: String) -> Entry? { entries[hash] }

    /// Take a negotiation's tickets, with the exported files in `directory`.
    ///
    /// The local file is under `ticket.filename` — `storedFilename` is the *service's* name for it
    /// in the trip's source folder and never a local path.
    public func enqueue(_ tickets: [UploadTicket], filesIn directory: URL) {
        self.fileDirectory = directory
        self.tickets = tickets
        for ticket in tickets {
            if let existing = entries[ticket.hash], existing.state == .uploaded { continue }
            entries[ticket.hash] = Entry(
                hash: ticket.hash,
                filename: ticket.filename,
                size: ticket.size,
                state: .pending,
                attempts: entries[ticket.hash]?.attempts ?? 0,
                bytesSent: entries[ticket.hash]?.bytesSent ?? 0,
                lastError: entries[ticket.hash]?.lastError,
                completedAt: nil)
        }
        persist()
    }

    /// Re-attach to uploads a previous process left running, before starting anything new.
    ///
    /// Returns the hashes adopted. A background session may well have finished them while the app
    /// was dead, in which case this is where that becomes known.
    @discardableResult
    public func adoptOutstanding() async -> [String] {
        let names = await transport.adoptOutstanding()
        var adopted: [String] = []
        for name in names where entries[name] != nil {
            adopted.append(name)
            do {
                let outcome = try await transport.awaitOutcome(named: name)
                record(outcome, for: name)
            } catch {
                fail(name, reason: "\(error)", retryable: true)
            }
        }
        if !adopted.isEmpty { persist() }
        return adopted
    }

    /// Send everything enqueued that is not already uploaded, one asset at a time.
    ///
    /// Sequential deliberately: the transfer is bandwidth-bound, and a failure that costs one file
    /// is the whole point of per-asset granularity.
    public func run() async -> Report {
        var report = Report()
        guard let directory = fileDirectory else { return report }

        for ticket in tickets {
            if entries[ticket.hash]?.state == .uploaded, entries[ticket.hash]?.completedAt != nil {
                report.alreadySentEarlier.append(ticket.hash)
                continue
            }
            if ticket.isExpired(at: Date()) == true {
                mark(ticket.hash, .needsRenegotiation, reason: "the presigned URL expired at \(ticket.expiresAtRaw)")
                report.needsRenegotiation.append(ticket.hash)
                continue
            }

            let file = directory.appending(path: ticket.filename)
            do {
                try verify(file, against: ticket)
            } catch {
                mark(ticket.hash, .failed, reason: "\(error)")
                report.failed[ticket.hash] = "\(error)"
                continue
            }

            var attempt = 0
            var settled = false
            while !settled {
                attempt += 1
                entries[ticket.hash]?.state = .uploading
                entries[ticket.hash]?.attempts = attempt
                persist()
                do {
                    let outcome = try await transport.send(
                        ticket.urlRequest(), fromFile: file, named: ticket.hash)
                    report.assetsSent += 1
                    if let bytes = outcome.bytesSent {
                        report.bytesMeasured += bytes
                    } else {
                        report.uploadsUnmeasured += 1
                    }
                    switch classify(outcome.statusCode) {
                    case .accepted:
                        record(outcome, for: ticket.hash)
                        report.uploaded.append(ticket.hash)
                        settled = true
                    case .renegotiate:
                        let reason =
                            "the store refused the grant (HTTP \(outcome.statusCode)): "
                            + (outcome.body ?? "no body")
                        mark(ticket.hash, .needsRenegotiation, reason: reason)
                        report.needsRenegotiation.append(ticket.hash)
                        settled = true
                    case .permanent:
                        let reason = "HTTP \(outcome.statusCode): " + (outcome.body ?? "no body")
                        mark(ticket.hash, .failed, reason: reason)
                        report.failed[ticket.hash] = reason
                        settled = true
                    case .transient:
                        if attempt >= maxAttempts {
                            let reason =
                                "HTTP \(outcome.statusCode) after \(attempt) attempts: "
                                + (outcome.body ?? "no body")
                            mark(ticket.hash, .failed, reason: reason)
                            report.failed[ticket.hash] = reason
                            settled = true
                        } else {
                            try? await Task.sleep(for: retryDelay)
                        }
                    }
                } catch {
                    if attempt >= maxAttempts {
                        mark(ticket.hash, .failed, reason: "\(error)")
                        report.failed[ticket.hash] = "\(error)"
                        settled = true
                    } else {
                        try? await Task.sleep(for: retryDelay)
                    }
                }
            }
            persist()
        }
        return report
    }

    // MARK: - Internals

    private enum Classification { case accepted, transient, permanent, renegotiate }

    private func classify(_ status: Int) -> Classification {
        switch status {
        case 200...299: .accepted
        // S3 answers 403 for an expired signature and for one it could not verify. Either way the
        // URL is spent, and retrying it is a slower way to fail.
        case 400, 403: .renegotiate
        case 408, 429, 500...599: .transient
        default: .permanent
        }
    }

    private func verify(_ file: URL, against ticket: UploadTicket) throws {
        let attributes = try? FileManager.default.attributesOfItem(atPath: file.path)
        guard let attributes, let size = (attributes[.size] as? NSNumber)?.intValue else {
            throw QueueError.fileMissing(hash: ticket.hash, path: file.path)
        }
        guard size == ticket.size else {
            throw QueueError.lengthMismatch(
                hash: ticket.hash, declared: ticket.size, onDisk: size)
        }
    }

    private func record(_ outcome: UploadOutcome, for hash: String) {
        guard var entry = entries[hash] else { return }
        if let bytes = outcome.bytesSent {
            entry.bytesSent = (entry.bytesSent ?? 0) + bytes
        }
        if (200...299).contains(outcome.statusCode) {
            entry.state = .uploaded
            entry.lastError = nil
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime]
            entry.completedAt = formatter.string(from: Date())
        } else {
            entry.state = .failed
            entry.lastError = "HTTP \(outcome.statusCode)"
        }
        entries[hash] = entry
    }

    private func fail(_ hash: String, reason: String, retryable: Bool) {
        mark(hash, retryable ? .pending : .failed, reason: reason)
    }

    private func mark(_ hash: String, _ state: AssetState, reason: String?) {
        guard var entry = entries[hash] else { return }
        entry.state = state
        entry.lastError = reason
        entries[hash] = entry
        persist()
    }

    /// After every asset, not at the end. The whole resume story rests on this line.
    private func persist() {
        let file = StateFile(entries: state)
        guard let data = try? JSONEncoder().encode(file) else { return }
        try? FileManager.default.createDirectory(
            at: stateURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: stateURL, options: .atomic)
    }
}
