import Foundation
import Testing

@testable import StoryService

/// Job bodies shaped exactly like `service/storybook_service/jobs.py::_job_json`.
enum JobFixtures {
    static func queued(ahead: Int = 0) -> [String: Any] {
        [
            "job_id": "job-1", "trip_id": "trip-1", "kind": "build", "state": "queued",
            "stage": NSNull(), "done": 0, "total": NSNull(),
            "stage_index": NSNull(), "stages_total": NSNull(),
            "attempts": 0, "created_at": "2026-08-10T09:00:00+00:00", "queued_ahead": ahead,
            "progress_basis":
                "queued: no work has started, so there is nothing measured. `total` is null rather than 0",
        ]
    }

    static func running(
        stage: String, index: Int, done: Int, total: Any, degraded: Bool = true
    ) -> [String: Any] {
        [
            "job_id": "job-1", "trip_id": "trip-1", "kind": "build", "state": "running",
            "stage": stage, "done": done, "total": total,
            "stage_index": index, "stages_total": 18, "stages_complete": index,
            "media_known": 11, "phase": "build", "attempts": 1,
            "created_at": "2026-08-10T09:00:00+00:00", "started_at": "2026-08-10T09:00:01+00:00",
            "degraded": degraded,
            "unavailable_stages": degraded
                ? [
                    [
                        "stage": "embeddings", "reason": "CLIP unavailable: missing torch, open_clip",
                        "affects": "semantic clustering of similar shots",
                    ],
                    [
                        "stage": "content_class",
                        "reason": "CLIP unavailable: missing torch, open_clip",
                        "affects": "screenshot and receipt classification",
                    ],
                ] : [],
            "capability_measured_at": "2026-08-10T09:00:01+00:00",
            "progress_basis": "rows in stage_result for this stage, over the stage's candidate set",
            "stages": [
                [
                    "name": stage, "version": 1, "state": "in_progress", "done": done,
                    "failed": 0, "total": total,
                ]
            ],
        ]
    }

    /// Shaped from what the **real service** returns, which is not what this fixture first assumed:
    /// a succeeded build has no stage with outstanding work, so `stage`, `stage_index`, `done` and
    /// `total` come back as `null`/`0` while `stages_total` and `stages_complete` stay real
    /// (`progress.py`'s "every stage is complete or unavailable" branch). The fake said
    /// `stage_index: 18`, and the difference hid a client bug for the length of the session.
    static func succeeded() -> [String: Any] {
        var body = running(stage: "timeline", index: 18, done: 11, total: 11)
        body["state"] = "succeeded"
        body["stage"] = NSNull()
        body["stage_index"] = NSNull()
        body["done"] = 0
        body["total"] = NSNull()
        body["stages_complete"] = 18
        body["finished_at"] = "2026-08-10T09:20:00+00:00"
        body["trip_json"] = "/srv/story/trip-1/out/trip.json"
        body["progress_basis"] =
            "every stage is complete or unavailable, so stage_result shows no outstanding work"
        return body
    }

    static func failed() -> [String: Any] {
        var body = running(stage: "metadata", index: 2, done: 3, total: 11)
        body["state"] = "failed"
        body["error"] = "exiftool: No such file or directory"
        body["exit_code"] = 1
        return body
    }
}

struct JobStatusDecodingTests {
    private func status(_ body: [String: Any]) async throws -> JobStatus {
        let fake = FakeService()
        fake.jobScript = [body]
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        return try await JobPoller(endpoint: endpoint, identity: identity, session: session)
            .job(id: "job-1")
    }

    /// `total` is nullable by design, and a null denominator must render as *unknown* — not as zero,
    /// which reads as "nothing to do", and not as full.
    @Test func aNullTotalIsUnknownRatherThanZero() async throws {
        let queued = try await status(JobFixtures.queued())
        #expect(queued.total == nil)
        guard case .unknown = queued.stageCount else {
            Issue.record("a queued job's stage count must be unknown, got \(queued.stageCount)")
            return
        }
        guard case .unknown = queued.overallPosition else {
            Issue.record("a queued job has no stage index, so no overall position")
            return
        }
    }

    /// The control for the test above: a measured total *is* counted, so `.unknown` is not simply
    /// what this type always says.
    @Test func aMeasuredTotalIsCounted() async throws {
        let running = try await status(
            JobFixtures.running(stage: "metadata", index: 2, done: 4, total: 11))
        #expect(running.stageCount == .counted(done: 4, total: 11))
        #expect(running.overallPosition == .counted(done: 2, total: 18))
    }

    /// A stage whose candidate set could not be counted carries a null total of its own, with the
    /// service's reason.
    @Test func aStageWithNoTotalCarriesItsReason() async throws {
        let running = try await status(
            JobFixtures.running(stage: "embeddings", index: 5, done: 0, total: NSNull()))
        #expect(running.stages.count == 1)
        guard case .unknown = running.stages[0].count else {
            Issue.record("a null per-stage total must be unknown")
            return
        }
    }

    /// **No percentage, no fraction, no ETA.** The service forbids those fields in its own suite;
    /// this asserts the client did not add them on the way through, which is where the temptation
    /// actually is. Mirror rather than prose, so adding such a property fails here.
    @Test func exposesNoPercentageOrEta() async throws {
        let running = try await status(
            JobFixtures.running(stage: "metadata", index: 2, done: 4, total: 11))
        let forbidden = ["percent", "percentage", "fraction", "eta", "progress", "remainingTime"]
        let names = Mirror(reflecting: running).children.compactMap(\.label)
        #expect(names.allSatisfy { !forbidden.contains($0) })
        // The control: the mirror can see the properties that do exist, so an empty list is not
        // why this passed.
        #expect(names.contains("stageIndex"))
    }

    /// A degraded build says so in the stage's own words. This deployment has no `clip` (Q18), so
    /// `embeddings` and `content_class` are unavailable and the reason is theirs, not a paraphrase.
    @Test func reportsADegradedBuildInTheStagesOwnWords() async throws {
        let running = try await status(
            JobFixtures.running(stage: "metadata", index: 2, done: 4, total: 11))
        #expect(running.degraded == true)
        #expect(running.unavailableStages.map(\.stage) == ["embeddings", "content_class"])
        #expect(running.degradedSummary[0].contains("CLIP unavailable: missing torch, open_clip"))
    }

    /// An unmeasured capability is `nil`, not `false`. "Nobody has looked yet" must not read as
    /// "as good as this pipeline gets".
    @Test func anUnmeasuredCapabilityIsNilRatherThanNotDegraded() async throws {
        let queued = try await status(JobFixtures.queued())
        #expect(queued.degraded == nil)
        #expect(queued.unavailableStages.isEmpty)
    }

    @Test func carriesTheBuildsOwnLastWordsOnFailure() async throws {
        let failed = try await status(JobFixtures.failed())
        #expect(failed.state == .failed)
        #expect(failed.error == "exiftool: No such file or directory")
        #expect(failed.exitCode == 1)
        #expect(failed.isTerminal)
    }

    /// A state this client does not know is not terminal, or `follow` would stop early and report a
    /// build as over on the strength of a word it could not read.
    @Test func anUnknownStateIsNotTerminal() async throws {
        var body = JobFixtures.queued()
        body["state"] = "paused"
        let status = try await status(body)
        #expect(status.state == nil)
        #expect(status.stateRaw == "paused")
        #expect(status.isTerminal == false)
    }

    @Test func reportsSomeoneElsesJobAsNotFound() async throws {
        let fake = FakeService()
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        // The fake answers a scripted job only for the ids it knows; an empty script means the
        // route replies with a queued job, so ask for a route it has none of instead.
        let poller = JobPoller(endpoint: endpoint, identity: identity, session: session)
        await #expect(throws: ServiceError.self) {
            _ = try await poller.jobs(tripID: "someone-elses-trip")
        }
    }
}

struct ProgressMonotonicityTests {
    private func status(index: Int, done: Int) -> JobStatus {
        JobStatus(
            jobID: "j", tripID: "t", kind: "build", stateRaw: "running", state: .running,
            stage: "metadata", done: done, total: 11, stageIndex: index, stagesTotal: 18,
            stagesComplete: index, mediaKnown: 11, phase: "build", attempts: 1, queuedAhead: nil,
            error: nil, exitCode: nil, degraded: false, unavailableStages: [],
            capabilityMeasuredAt: nil, progressBasis: nil, progressDetail: nil, stages: [],
            tripJSONPath: nil)
    }

    @Test func anAdvancingSequenceNeverFlagsARegression() {
        var monotonicity = ProgressMonotonicity()
        let flags = [(1, 0), (1, 5), (1, 11), (2, 0), (2, 3)].map {
            monotonicity.admit(status(index: $0.0, done: $0.1))
        }
        #expect(flags.allSatisfy { $0 == false })
    }

    /// The control the criterion needs: a sequence that *does* go backwards is flagged. Without it,
    /// "monotonically advancing" would be a property of a detector that never fires.
    @Test func aRegressionIsFlagged() {
        var monotonicity = ProgressMonotonicity()
        _ = monotonicity.admit(status(index: 3, done: 8))
        #expect(monotonicity.admit(status(index: 3, done: 4)) == true)
    }

    @Test func aStageGoingBackwardsIsFlagged() {
        var monotonicity = ProgressMonotonicity()
        _ = monotonicity.admit(status(index: 4, done: 0))
        #expect(monotonicity.admit(status(index: 3, done: 11)) == true)
    }

    /// `done` resetting to 0 as the stage advances is not a regression — each stage counts its own
    /// items. Getting this wrong would flag every normal build.
    @Test func doneResettingOnANewStageIsNotARegression() {
        var monotonicity = ProgressMonotonicity()
        _ = monotonicity.admit(status(index: 3, done: 11))
        #expect(monotonicity.admit(status(index: 4, done: 0)) == false)
    }

    /// **The bug the live run found.** A succeeded build publishes `stage_index: null`, because no
    /// stage has outstanding work. Substituting a sentinel for that absence flagged the final
    /// reading of every healthy build as a regression.
    @Test func aReadingWithNoStageIndexIsNotARegression() {
        var monotonicity = ProgressMonotonicity()
        _ = monotonicity.admit(status(index: 17, done: 4))
        var terminal = status(index: 0, done: 0)
        terminal = JobStatus(
            jobID: terminal.jobID, tripID: terminal.tripID, kind: terminal.kind,
            stateRaw: "succeeded", state: .succeeded, stage: nil, done: 0, total: nil,
            stageIndex: nil, stagesTotal: 18, stagesComplete: 18, mediaKnown: 11, phase: "build",
            attempts: 1, queuedAhead: nil, error: nil, exitCode: nil, degraded: false,
            unavailableStages: [], capabilityMeasuredAt: nil, progressBasis: nil,
            progressDetail: nil, stages: [], tripJSONPath: "/out/trip.json")
        #expect(monotonicity.admit(terminal) == false)
        // And it is not a new baseline either: the next real reading is still compared against 17,
        // so a genuine regression after an unmeasured reading is still caught.
        #expect(monotonicity.admit(status(index: 5, done: 0)) == true)
    }
}

struct JobFollowTests {
    @Test func startsABuildAndReportsItQueued() async throws {
        let fake = FakeService()
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        let start = try await JobPoller(endpoint: endpoint, identity: identity, session: session)
            .startBuild(tripID: "trip-1")
        #expect(start.created == true)
        #expect(start.job.state == .queued)
    }

    @Test func followsAJobToSuccessAndStops() async throws {
        let fake = FakeService()
        fake.jobScript = [
            JobFixtures.queued(),
            JobFixtures.running(stage: "metadata", index: 2, done: 4, total: 11),
            JobFixtures.running(stage: "geocode", index: 5, done: 11, total: 11),
            JobFixtures.succeeded(),
        ]
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        let poller = JobPoller(endpoint: endpoint, identity: identity, session: session)

        var readings: [JobReading] = []
        for try await reading in poller.follow(jobID: "job-1", every: .zeroForTests) {
            readings.append(reading)
        }

        #expect(readings.count == 4)
        #expect(readings.last?.status.state == .succeeded)
        #expect(readings.last?.status.tripJSONPath == "/srv/story/trip-1/out/trip.json")
        #expect(readings.allSatisfy { !$0.wentBackwards })
    }

    @Test func followsAJobToFailureAndStops() async throws {
        let fake = FakeService()
        fake.jobScript = [
            JobFixtures.running(stage: "metadata", index: 2, done: 3, total: 11),
            JobFixtures.failed(),
        ]
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        let poller = JobPoller(endpoint: endpoint, identity: identity, session: session)

        var last: JobReading?
        for try await reading in poller.follow(jobID: "job-1", every: .zeroForTests) {
            last = reading
        }
        #expect(last?.status.state == .failed)
    }

    /// A regression in the *service's* answers is surfaced, not smoothed. Clamping would make a
    /// resumed build look stalled and hide the only evidence that something is wrong.
    @Test func surfacesARegressionRatherThanSmoothingIt() async throws {
        let fake = FakeService()
        fake.jobScript = [
            JobFixtures.running(stage: "geocode", index: 5, done: 9, total: 11),
            JobFixtures.running(stage: "geocode", index: 5, done: 2, total: 11),
            JobFixtures.succeeded(),
        ]
        let (endpoint, session, identity) = fake.install()
        defer { fake.uninstall() }
        let poller = JobPoller(endpoint: endpoint, identity: identity, session: session)

        var readings: [JobReading] = []
        for try await reading in poller.follow(jobID: "job-1", every: .zeroForTests) {
            readings.append(reading)
        }
        #expect(readings.map(\.wentBackwards) == [false, true, false])
        // And the reading itself is untouched: 2, not clamped up to 9.
        #expect(readings[1].status.done == 2)
    }
}
