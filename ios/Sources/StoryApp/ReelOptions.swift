import Foundation
import StoryKit
import StoryService

/// I30. Aspect, day range, music, name, subtitles → `POST /trips/{id}/reel`, then real progress —
/// never a fabricated percentage, same as `LoopScreen`'s build tracking.
///
/// **This file owns no business logic beyond assembling the request and reading the answer.**
/// Everything about what a reel actually contains is S07's; everything about polling `GET
/// /jobs/{id}` is I22's `JobPoller`, reused here unchanged because a reel is a second job *kind*,
/// not a second mechanism — its `state`/`stage`/`done`/`total` come back through the exact same
/// route and the exact same `JobStatus` type a build does. Only two routes have no existing
/// client: `POST /trips/{id}/reel` (a body, unlike a build's empty one) and `GET /jobs/{id}/reel`
/// (the finished video). Both are written out here against the public `ServiceEndpoint`/
/// `ServiceIdentity` types, the same choice `LoopScreen` made for `GET /jobs/{id}/report` and `GET
/// /trips/{id}/media/{relpath}` — `ServiceHTTP` is internal to `StoryService`, and adding to
/// `JobPoller`/`NegotiateClient` would touch a module this task does not own.

// MARK: - The request

/// Mirrors `service/storybook_service/jobs.py`'s `ReelRequest` field for field. Every option here
/// reaches the wire exactly as set — there is no client-side default that silently overrides what
/// a screen left unset; an absent field is encoded as JSON `null` (`encodeIfPresent` is
/// deliberately not used), and the service documents its own defaults for `nil`.
public struct ReelOptions: Sendable, Equatable, Encodable {
    public var aspect: String?
    public var musicHash: String?
    public var day: String?
    public var dateFrom: String?
    public var dateTo: String?
    public var places: [String] = []
    public var name: String?
    public var subtitles: [String] = []
    public var burnIn: String?
    public var clipAudio: Bool?

    public init(
        aspect: String? = nil, musicHash: String? = nil, day: String? = nil,
        dateFrom: String? = nil, dateTo: String? = nil, places: [String] = [],
        name: String? = nil, subtitles: [String] = [], burnIn: String? = nil,
        clipAudio: Bool? = nil
    ) {
        self.aspect = aspect
        self.musicHash = musicHash
        self.day = day
        self.dateFrom = dateFrom
        self.dateTo = dateTo
        self.places = places
        self.name = name
        self.subtitles = subtitles
        self.burnIn = burnIn
        self.clipAudio = clipAudio
    }

    enum CodingKeys: String, CodingKey {
        case aspect, day, places, name, subtitles
        case musicHash = "music_hash"
        case dateFrom = "date_from"
        case dateTo = "date_to"
        case burnIn = "burn_in"
        case clipAudio = "clip_audio"
    }
}

/// Presets a screen may offer. The service accepts any `"W:H"` string (`parse_aspect` just wants
/// two positive integers) — these are not a validated enum, only a convenience list.
public enum ReelAspectPreset: String, Sendable, CaseIterable {
    case widescreen = "16:9"
    case portrait = "9:16"
    case square = "1:1"
    case classicPortrait = "4:5"
}

// MARK: - What comes back

public struct ReelStart: Sendable, Equatable {
    public let jobID: String
    public let tripID: String
    public let stateRaw: String
    /// `false` when this trip already had a job (build or reel) queued or running, and *that* job
    /// came back instead of a new one — matching `BuildStart.created`'s own meaning.
    public let created: Bool
    public let detail: String?
}

/// The manifest at `GET /jobs/{id}/reel`'s `reel_json` key, kept as a JSON object rather than a
/// fixed set of Swift properties. It is not this client's schema to pin down — it is
/// `story-book reel`'s own published artifact — and I30's acceptance criterion only needs to read
/// specific keys back, never to model the whole shape.
public struct ReelManifest: Equatable, @unchecked Sendable {
    public let raw: [String: Any]

    public init(raw: [String: Any]) { self.raw = raw }

    public subscript(key: String) -> Any? { raw[key] }

    /// Dotted-path lookup for the nested shape `write_reel_json` actually produces, e.g.
    /// `"video.aspect"` or `"audio.music_supplied"`.
    public func value(at path: String) -> Any? {
        var current: Any? = raw
        for component in path.split(separator: ".") {
            guard let dict = current as? [String: Any] else { return nil }
            current = dict[String(component)]
        }
        return current
    }

    public static func == (lhs: ReelManifest, rhs: ReelManifest) -> Bool {
        NSDictionary(dictionary: lhs.raw).isEqual(to: rhs.raw)
    }
}

public struct ReelDownload: Sendable, Equatable {
    public let jobID: String
    public let tripID: String
    public let state: String
    public let videoDownloadURL: URL
    public let videoExpiresAtRaw: String
    public let videoSizeBytes: Int
    public let immutable: Bool
    public let manifest: ReelManifest
}

// MARK: - The client

/// The two routes `JobPoller`/`NegotiateClient` do not cover. Polling itself — `GET /jobs/{id}` —
/// is `JobPoller.job(id:)`/`.follow(jobID:)`, reused as-is: a reel's `state`/`stage`/`done`/`total`
/// come back through that exact route with `kind: "reel"` and a different `stage` string
/// (`"reel:render"`), which `JobStatus` already carries.
public struct ReelClient: Sendable {
    private let endpoint: ServiceEndpoint
    private let identity: any ServiceIdentity
    private let session: URLSession

    public init(
        endpoint: ServiceEndpoint, identity: any ServiceIdentity, session: URLSession = .shared
    ) {
        self.endpoint = endpoint
        self.identity = identity
        self.session = session
    }

    /// A bad `aspect` or an undeclared `music_hash` is a 422 here, before any job is queued —
    /// surfaced as `ServiceError.rejected(status: 422, detail:)`, the service's own words.
    public func startReel(tripID: String, options: ReelOptions) async throws -> ReelStart {
        var request = URLRequest(url: endpoint.url("/trips/\(tripID)/reel"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        for (name, value) in try await identity.identityHeaders() {
            request.setValue(value, forHTTPHeaderField: name)
        }
        request.httpBody = try JSONEncoder().encode(options)

        let (data, _) = try await send(request)
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let jobID = object["job_id"] as? String,
            let tripIDReturned = object["trip_id"] as? String,
            let state = object["state"] as? String
        else {
            throw ServiceError.malformedResponse("POST /trips/\(tripID)/reel: unexpected body")
        }
        return ReelStart(
            jobID: jobID, tripID: tripIDReturned, stateRaw: state,
            created: (object["created"] as? Bool) ?? true,
            detail: object["detail"] as? String)
    }

    /// `404` if `jobID` names a build, not a reel; `409` while the reel is still queued or
    /// running — both surface as `ServiceError.rejected`/`.notFound` with the service's own text,
    /// which already says to poll `GET /jobs/{id}` until `state` is `succeeded`.
    public func reelDownload(jobID: String) async throws -> ReelDownload {
        var request = URLRequest(url: endpoint.url("/jobs/\(jobID)/reel"))
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        for (name, value) in try await identity.identityHeaders() {
            request.setValue(value, forHTTPHeaderField: name)
        }
        let (data, _) = try await send(request)
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let jobID = object["job_id"] as? String,
            let tripID = object["trip_id"] as? String,
            let state = object["state"] as? String,
            let video = object["video"] as? [String: Any],
            let downloadURLString = video["download_url"] as? String,
            let downloadURL = URL(string: downloadURLString),
            let expiresAt = video["expires_at"] as? String,
            let sizeBytes = video["size_bytes"] as? Int,
            let immutable = object["immutable"] as? Bool
        else {
            throw ServiceError.malformedResponse("GET /jobs/\(jobID)/reel: unexpected body")
        }
        let manifest = (object["reel_json"] as? [String: Any]) ?? [:]
        return ReelDownload(
            jobID: jobID, tripID: tripID, state: state, videoDownloadURL: downloadURL,
            videoExpiresAtRaw: expiresAt, videoSizeBytes: sizeBytes, immutable: immutable,
            manifest: ReelManifest(raw: manifest))
    }

    private func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw ServiceError.transport("\(request.httpMethod ?? "GET") \(request.url?.path ?? ""): \(error)")
        }
        guard let http = response as? HTTPURLResponse else {
            throw ServiceError.malformedResponse("no HTTP response from \(request.url?.absoluteString ?? "?")")
        }
        guard (200...299).contains(http.statusCode) else {
            let detail = Self.detail(in: data)
            switch http.statusCode {
            case 401: throw ServiceError.unauthorized(detail)
            case 404: throw ServiceError.notFound(detail)
            default: throw ServiceError.rejected(status: http.statusCode, detail: detail)
            }
        }
        return (data, http)
    }

    private static func detail(in data: Data) -> String {
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

// MARK: - Retention: "offer these while the media is still there"

/// `ios_backend_service.md`'s retention section: uploaded media is deleted **one month after the
/// most recent generation** of a build, report, package or reel, and the clock resets on every
/// one. After expiry a re-cut falls back to 720p proxies and is measurably softer (`01a9360`).
///
/// **No countdown is computed here.** `S04` (the sweeper that actually enforces this) is not
/// built yet, and neither `GET /trips/{id}` nor any job route publishes a deadline — there is no
/// wire field to read one from. Inventing a date from "one month after created_at" would be
/// exactly the fabricated-measurement failure this project keeps naming: this app has no evidence
/// of when the trip's media last regenerated. So the advisory below is static and unconditional —
/// true every time, informative rather than a false countdown — and becomes a real one the day
/// S04 (or a wire field it adds) gives this something to measure.
public enum ReelRetentionAdvisory {
    public static let text =
        "Uploaded photos and clips are kept for about a month after the most recent build or "
        + "reel, and every new one resets that clock. After they age out, a re-cut falls back to "
        + "lower-resolution proxies and looks visibly softer. Render the reel you want while the "
        + "originals are still here."
}

private extension String {
    var trimmedOrNil: String? {
        let value = trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }
    var splitOnCommasTrimmed: [String] {
        split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }
}

#if os(iOS)

    import AVFoundation
    import AVKit
    import SwiftUI

    /// `AVPlayerViewController` directly, not SwiftUI's `VideoPlayer` — the fullscreen expand
    /// control asked for here is `AVPlayerViewController`'s own built-in affordance, present in
    /// its default control bar, and `VideoPlayer` does not expose a way to confirm or configure
    /// it. Keyed to `url` (not recreated on every body evaluation) so a rebuild of the enclosing
    /// `Form` does not tear down and restart playback from zero.
    @available(iOS 17.0, *)
    private struct ReelPlayerView: UIViewControllerRepresentable {
        let url: URL

        func makeUIViewController(context: Context) -> AVPlayerViewController {
            // The default audio session category (`.soloAmbient`) is silenced by the ringer/
            // silent switch — a video with a real audio track then plays with no sound and no
            // error, which is exactly what was reported. `.playback` is what every video app uses
            // to be heard regardless of that switch; failure here is not fatal to playback itself,
            // only to whether it can be heard, so it is not surfaced as an error.
            try? AVAudioSession.sharedInstance().setCategory(.playback)
            try? AVAudioSession.sharedInstance().setActive(true)

            let controller = AVPlayerViewController()
            controller.player = AVPlayer(url: url)
            controller.entersFullScreenWhenPlaybackBegins = false
            controller.exitsFullScreenWhenPlaybackEnds = true
            return controller
        }

        func updateUIViewController(_ controller: AVPlayerViewController, context: Context) {}
    }

    /// The screen. **Owns no logic beyond what belongs to a view**: `ReelOptionsModel` builds the
    /// request and reads the answer; `MusicImportSection` (I31) is embedded, not reimplemented.
    @available(iOS 17.0, *)
    @MainActor
    @Observable
    public final class ReelOptionsModel {
        public enum RangeMode: String, CaseIterable, Sendable {
            case wholeTrip = "Whole trip"
            case singleDay = "Single day"
            case dateRange = "Date range"
        }

        public enum Phase: Equatable {
            case idle
            case starting
            case polling(JobStatus)
            case fetchingDownload
            case ready(ReelDownload)
            case failed(String)
        }

        // Form state. Every one of these maps directly to a `ReelOptions` field, so the acceptance
        // criterion ("each option reaches the service and is reflected in reel.json") is provable
        // by reading `buildOptions()` and the manifest side by side — nothing here is transformed
        // into something the service was not asked for.
        public var aspect: ReelAspectPreset = .widescreen
        public var rangeMode: RangeMode = .wholeTrip
        public var day: String = ""
        public var dateFrom: String = ""
        public var dateTo: String = ""
        public var places: String = ""
        public var name: String = ""
        public var subtitles: String = ""
        public var burnIn: String = ""
        public var clipAudio: Bool = true

        public let music: MusicImportModel
        public var phase: Phase = .idle
        public var errorMessage: String?

        private let endpoint: ServiceEndpoint
        private let identity: any ServiceIdentity
        private let tripID: String
        private let session: URLSession
        private let client: ReelClient

        public init(
            endpoint: ServiceEndpoint, identity: any ServiceIdentity, tripID: String,
            musicWorkingDirectory: URL, session: URLSession = .shared
        ) {
            self.endpoint = endpoint
            self.identity = identity
            self.tripID = tripID
            self.session = session
            self.client = ReelClient(endpoint: endpoint, identity: identity, session: session)
            self.music = MusicImportModel(
                client: StoryServiceClient(endpoint: endpoint, identity: identity, session: session),
                tripID: tripID, workingDirectory: musicWorkingDirectory)
        }

        public var isRunning: Bool {
            switch phase {
            case .idle, .ready, .failed: false
            default: true
            }
        }

        /// Exposed so a test (or a confirmation screen) can inspect exactly what will be sent,
        /// without having to run the network call to find out.
        public func buildOptions() -> ReelOptions {
            var options = ReelOptions()
            options.aspect = aspect.rawValue
            options.musicHash = music.musicHash
            switch rangeMode {
            case .wholeTrip:
                break
            case .singleDay:
                options.day = day.trimmedOrNil
            case .dateRange:
                options.dateFrom = dateFrom.trimmedOrNil
                options.dateTo = dateTo.trimmedOrNil
            }
            options.places = places.splitOnCommasTrimmed
            options.name = name.trimmedOrNil
            options.subtitles = subtitles.splitOnCommasTrimmed
            options.burnIn = burnIn.trimmedOrNil
            options.clipAudio = clipAudio
            return options
        }

        /// Submit, then track exactly the way `LoopScreen` tracks a build: real `state`/`stage`/
        /// `done`/`total`/`degraded` from `GET /jobs/{id}`, via the same `JobPoller` that route
        /// already has a client for — a reel needs no polling code of its own.
        public func submit() async {
            errorMessage = nil
            phase = .starting
            do {
                let start = try await client.startReel(tripID: tripID, options: buildOptions())
                let poller = JobPoller(endpoint: endpoint, identity: identity, session: session)
                var last: JobStatus?
                for try await reading in poller.follow(jobID: start.jobID) {
                    phase = .polling(reading.status)
                    last = reading.status
                }
                guard let last else {
                    throw ServiceError.transport("no reading came back for job \(start.jobID)")
                }
                guard last.state == .succeeded else {
                    throw ServiceError.rejected(
                        status: 0,
                        detail: last.error ?? "the reel job ended in state \(last.stateRaw)")
                }
                phase = .fetchingDownload
                let download = try await client.reelDownload(jobID: start.jobID)
                phase = .ready(download)
            } catch {
                let message = "\(error)"
                errorMessage = message
                phase = .failed(message)
            }
        }
    }

    @available(iOS 17.0, *)
    public struct ReelOptionsScreen: View {
        @State private var model: ReelOptionsModel

        public init(model: ReelOptionsModel) {
            _model = State(initialValue: model)
        }

        public var body: some View {
            Form {
                Section("Aspect") {
                    Picker("Aspect", selection: $model.aspect) {
                        ForEach(ReelAspectPreset.allCases, id: \.self) { preset in
                            Text(preset.rawValue).tag(preset)
                        }
                    }
                }
                Section("Range") {
                    Picker("Range", selection: $model.rangeMode) {
                        ForEach(ReelOptionsModel.RangeMode.allCases, id: \.self) { mode in
                            Text(mode.rawValue).tag(mode)
                        }
                    }
                    switch model.rangeMode {
                    case .wholeTrip:
                        EmptyView()
                    case .singleDay:
                        TextField("YYYY-MM-DD", text: $model.day)
                            .keyboardType(.numbersAndPunctuation)
                    case .dateRange:
                        TextField("From YYYY-MM-DD", text: $model.dateFrom)
                            .keyboardType(.numbersAndPunctuation)
                        TextField("To YYYY-MM-DD", text: $model.dateTo)
                            .keyboardType(.numbersAndPunctuation)
                    }
                    TextField("Places, comma separated (optional)", text: $model.places)
                }
                Section("Name") {
                    TextField("Title card and filename (optional)", text: $model.name)
                }
                MusicImportSection(model: model.music)
                Section("Subtitles") {
                    TextField("Languages, comma separated (optional)", text: $model.subtitles)
                    TextField("Burn in language (optional)", text: $model.burnIn)
                    Toggle("Play clips' own sound", isOn: $model.clipAudio)
                }
                Section {
                    Button(model.isRunning ? "Rendering…" : "Render reel") {
                        Task { await model.submit() }
                    }
                    .disabled(model.isRunning)
                    phaseView
                } footer: {
                    Text(ReelRetentionAdvisory.text)
                }
            }
            .navigationTitle("New reel")
        }

        @ViewBuilder
        private var phaseView: some View {
            switch model.phase {
            case .idle:
                EmptyView()
            case .starting:
                LabeledContent("Reel", value: "starting…")
            case .polling(let status):
                pollingView(status)
            case .fetchingDownload:
                LabeledContent("Reel", value: "fetching the finished video…")
            case .ready(let download):
                LabeledContent("Reel", value: "ready — \(download.videoSizeBytes) bytes")
                // Plays in-app, directly against the signed URL — the mux already re-applies
                // `+faststart` (9eb53ab), so a progressive MP4 starts before it has fully arrived,
                // same premise I32 itself will build on. **What this is not:** I32's own
                // criterion also asks for a background download so a *second* play makes no
                // network request, and this does not cache anything or reuse `MediaCache` (I33)
                // — a fresh `AVPlayer` re-streams from the same signed URL every time this view
                // appears. That gap is I32's to close, not silently claimed here.
                ReelPlayerView(url: download.videoDownloadURL)
                    .frame(height: 220)
                ShareLink("Share or save", item: download.videoDownloadURL)
            case .failed(let reason):
                Text(reason).font(.footnote).foregroundStyle(.red)
            }
        }

        @ViewBuilder
        private func pollingView(_ status: JobStatus) -> some View {
            LabeledContent("State", value: status.stateRaw)
            if let stage = status.stage {
                LabeledContent("Stage", value: stage)
            }
            switch status.stageCount {
            case .counted(let done, let total):
                ProgressView(value: Double(done), total: Double(max(total, 1))) {
                    Text("\(done) of \(total) segments rendered")
                }
            case .unknown(let reason):
                Text(reason).font(.footnote).foregroundStyle(.secondary)
            }
            if status.degraded == true, !status.degradedSummary.isEmpty {
                ForEach(status.degradedSummary, id: \.self) { line in
                    Text(line).font(.footnote).foregroundStyle(.orange)
                }
            }
            if let error = status.error {
                Text(error).font(.footnote).foregroundStyle(.red)
            }
        }
    }

#endif

