import Foundation

/// I22. Start a build and follow it.
///
/// **Progress here is real or it is absent.** Every count the service publishes is a row count in
/// that trip's own `story.db`, read with the pipeline's own accessors — and the service publishes
/// **no percentage, no fraction and no ETA**, deliberately: eighteen stages cost wildly different
/// amounts per item, so one number over all of them is a measurement nobody took. S03 went as far
/// as a test forbidding a field named `percent`, `fraction` or `eta`.
///
/// This client does not compute one either. `total` is **nullable**, and a null denominator renders
/// as *unknown* — never as zero, never as full. That is what ``StageCount`` is for.
///
/// Polling, not push: APNs is Wave 4. Nothing here fabricates smoothing between readings.

// MARK: - Progress, without inventing any

/// A count that may not exist. The point of the type is that there is no way to spell "0 of 0" for
/// something unmeasured.
public enum StageCount: Sendable, Equatable {
    case counted(done: Int, total: Int)
    case unknown(reason: String)

    public var isKnown: Bool {
        if case .counted = self { return true }
        return false
    }
}

public struct UnavailableStage: Sendable, Equatable, Decodable {
    /// The stage that cannot run in this deployment.
    public let stage: String
    /// **Its own words**, from `Stage.available()`, e.g. *"CLIP unavailable: missing torch,
    /// open_clip"*. Not paraphrased here.
    public let reason: String
    /// What the absence costs, from the stage's own `description`.
    public let affects: String?

    enum CodingKeys: String, CodingKey { case stage, reason, affects }
}

public struct StageProgress: Sendable, Equatable {
    public let name: String
    public let version: Int?
    /// `complete`, `in_progress`, `pending`, `unavailable`, or `unknown` — the service's word.
    public let state: String
    public let done: Int
    public let failed: Int
    public let total: Int?
    public let detail: String?

    public var count: StageCount {
        guard let total else {
            return .unknown(
                reason: detail ?? "the service reported no total for \(name), so nothing has been measured")
        }
        return .counted(done: done, total: total)
    }
}

public struct JobStatus: Sendable, Equatable {
    public enum State: String, Sendable, Equatable {
        case queued, running, succeeded, failed
    }

    public let jobID: String
    public let tripID: String
    public let kind: String?
    /// The raw string, always. `state` is `nil` for a value this client does not know, and an
    /// unknown state is **not** treated as terminal.
    public let stateRaw: String
    public let state: State?
    /// `nil` while queued; `source:prepare` while the media is being fetched; otherwise the first
    /// pipeline stage with outstanding work.
    public let stage: String?
    public let done: Int?
    public let total: Int?
    public let stageIndex: Int?
    public let stagesTotal: Int?
    public let stagesComplete: Int?
    public let mediaKnown: Int?
    public let phase: String?
    public let attempts: Int?
    public let queuedAhead: Int?
    public let error: String?
    public let exitCode: Int?
    /// `true` when some stage could not run in this deployment. Today's image has no `clip`
    /// (ratified in Q18), so a real build reports `embeddings` and `content_class` unavailable.
    /// `nil` means the job had not measured capability yet — not "as good as it gets".
    public let degraded: Bool?
    public let unavailableStages: [UnavailableStage]
    public let capabilityMeasuredAt: String?
    /// The service's own sentence about what its numbers are counted from. Worth showing a user
    /// verbatim when they ask what "4 of 18" means.
    public let progressBasis: String?
    public let progressDetail: String?
    public let stages: [StageProgress]
    /// Path to `trip.json` **on the service's filesystem**, present once succeeded. It is not a
    /// URL the app can fetch; delivery is S05.
    public let tripJSONPath: String?

    public var isTerminal: Bool { state == .succeeded || state == .failed }

    /// Work inside the current stage. `.unknown` where the service measured nothing.
    public var stageCount: StageCount {
        guard let total else {
            return .unknown(
                reason: progressBasis
                    ?? "the service reported no total, so the size of this step is not known")
        }
        return .counted(done: done ?? 0, total: total)
    }

    /// Position across the pipeline: the honest overall reading, and still not a percentage.
    public var overallPosition: StageCount {
        guard let stageIndex, let stagesTotal else {
            return .unknown(
                reason: progressBasis
                    ?? "no stage index yet: the build has not reached the pipeline")
        }
        return .counted(done: stageIndex, total: stagesTotal)
    }

    /// One line per stage this deployment cannot run, in the stage's own words. Empty when the
    /// build was as good as this pipeline gets — and empty is also what an unmeasured job reports,
    /// which is why ``degraded`` is a separate optional.
    public var degradedSummary: [String] {
        unavailableStages.map { entry in
            [entry.stage, entry.reason, entry.affects].compactMap { $0 }.joined(separator: " — ")
        }
    }
}

public struct BuildStart: Sendable, Equatable {
    public let job: JobStatus
    /// `false` when this trip already had a job queued or running, and *that* job came back. Not an
    /// error: the app that lost its `job_id` to a relaunch needs it back, and one `story.db` cannot
    /// be built twice at once.
    public let created: Bool
    public let detail: String?
}

/// One poll, with whether it went backwards.
public struct JobReading: Sendable, Equatable {
    public let status: JobStatus
    /// True when this reading is *behind* the one before it.
    ///
    /// The regression is **reported, never smoothed**. Clamping to the previous value would make a
    /// resumed-from-scratch build look like a stalled one, and the service's own suite asserts its
    /// counts do not go backwards — so if this is ever true, something is worth knowing about.
    public let wentBackwards: Bool
}

/// Compares consecutive readings. Split out so the rule is testable without a server.
///
/// **Only readings that carry a position are compared.** A queued job and a *succeeded* one both
/// publish `stage_index: null` — a succeeded build has no stage with outstanding work — and the
/// first version of this substituted `-1` for the missing value, so the final reading of a
/// perfectly healthy build was flagged as having gone backwards. Found by running the loop against
/// the real service, not by any of the fake's scripts. An absent measurement is not a low one, and
/// it is not a new baseline either: the next real reading is compared against the last real one.
public struct ProgressMonotonicity: Sendable {
    private var last: (stageIndex: Int, done: Int)?

    public init() {}

    public mutating func admit(_ status: JobStatus) -> Bool {
        guard let stageIndex = status.stageIndex else { return false }
        let current = (stageIndex: stageIndex, done: status.done ?? 0)
        defer { last = current }
        guard let last else { return false }
        if current.stageIndex < last.stageIndex { return true }
        if current.stageIndex == last.stageIndex, current.done < last.done { return true }
        return false
    }
}

// MARK: - The client

public struct JobPoller: Sendable {
    private let http: ServiceHTTP

    public init(
        endpoint: ServiceEndpoint,
        identity: any ServiceIdentity,
        session: URLSession = .shared
    ) {
        self.http = ServiceHTTP(endpoint: endpoint, identity: identity, session: session)
    }

    /// Queue a build of the whole trip. **There is no smaller unit** — no day, no subset — and the
    /// route takes no body: the config a build uses is the one `story-book init` measured from the
    /// uploaded media, and accepting one from the phone would replace a measurement with a cached
    /// guess, permanently.
    public func startBuild(tripID: String) async throws -> BuildStart {
        let wire = try await http.post("/trips/\(tripID)/build", as: JobWire.self)
        return BuildStart(job: wire.status, created: wire.created ?? true, detail: wire.detail)
    }

    public func job(id: String) async throws -> JobStatus {
        try await http.get("/jobs/\(id)", as: JobWire.self).status
    }

    /// Every job for a trip, newest first. This is how an app that was killed finds the build it
    /// was following without starting a second one.
    public func jobs(tripID: String) async throws -> [JobStatus] {
        try await http.get("/trips/\(tripID)/jobs", as: JobListWire.self).jobs.map(\.status)
    }

    /// Poll until the job reaches a terminal state, yielding every reading.
    ///
    /// A transport failure between readings does not end the stream — a phone loses its network
    /// constantly and the build carries on without it. Anything else (401, 404) does, because
    /// polling harder will not fix it.
    public func follow(
        jobID: String,
        every interval: Duration = .seconds(2),
        clock: any Clock<Duration> = ContinuousClock()
    ) -> AsyncThrowingStream<JobReading, any Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                var monotonicity = ProgressMonotonicity()
                while !Task.isCancelled {
                    do {
                        let status = try await job(id: jobID)
                        let backwards = monotonicity.admit(status)
                        continuation.yield(JobReading(status: status, wentBackwards: backwards))
                        if status.isTerminal {
                            continuation.finish()
                            return
                        }
                    } catch let error as ServiceError where error.isRetryable {
                        // Keep polling: the build is on the service, not on this connection.
                    } catch {
                        continuation.finish(throwing: error)
                        return
                    }
                    do {
                        try await clock.sleep(for: interval)
                    } catch {
                        continuation.finish()
                        return
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}

// MARK: - Wire shapes

private struct JobWire: Decodable, Sendable {
    struct Stage: Decodable, Sendable {
        let name: String
        let version: Int?
        let state: String
        let done: Int
        let failed: Int
        let total: Int?
        let detail: String?
    }

    let job_id: String
    let trip_id: String
    let kind: String?
    let state: String
    let stage: String?
    let done: Int?
    let total: Int?
    let stage_index: Int?
    let stages_total: Int?
    let stages_complete: Int?
    let media_known: Int?
    let phase: String?
    let attempts: Int?
    let queued_ahead: Int?
    let error: String?
    let exit_code: Int?
    let degraded: Bool?
    let unavailable_stages: [UnavailableStage]?
    let capability_measured_at: String?
    let progress_basis: String?
    let progress_detail: String?
    let stages: [Stage]?
    let trip_json: String?
    let created: Bool?
    let detail: String?

    var status: JobStatus {
        JobStatus(
            jobID: job_id,
            tripID: trip_id,
            kind: kind,
            stateRaw: state,
            state: JobStatus.State(rawValue: state),
            stage: stage,
            done: done,
            total: total,
            stageIndex: stage_index,
            stagesTotal: stages_total,
            stagesComplete: stages_complete,
            mediaKnown: media_known,
            phase: phase,
            attempts: attempts,
            queuedAhead: queued_ahead,
            error: error,
            exitCode: exit_code,
            degraded: degraded,
            unavailableStages: unavailable_stages ?? [],
            capabilityMeasuredAt: capability_measured_at,
            progressBasis: progress_basis,
            progressDetail: progress_detail,
            stages: (stages ?? []).map {
                StageProgress(
                    name: $0.name, version: $0.version, state: $0.state, done: $0.done,
                    failed: $0.failed, total: $0.total, detail: $0.detail)
            },
            tripJSONPath: trip_json)
    }
}

private struct JobListWire: Decodable, Sendable {
    let trip_id: String
    let jobs: [JobWire]
}
