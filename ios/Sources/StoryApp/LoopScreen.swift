import Compression
import Foundation

// MARK: - A minimal ZIP reader

/// Just enough of the ZIP format to unpack what `delivery.py` produces: STORE or DEFLATE entries,
/// no encryption, no zip64 (the report is html/css/vendored-Leaflet — "tens of kilobytes" per
/// `service/README.md`, never near the 4 GB where zip64 would matter).
///
/// Written rather than imported because adding a zip library is a `Package.swift` dependency
/// change, and that file belongs to I01, not I26. DEFLATE decoding uses the system `Compression`
/// framework — a system framework needs no package dependency, just an import.
///
/// Kept outside the `#if os(iOS)` guard below, deliberately: it touches no UIKit, no Photos, no
/// SwiftUI, so it is real, hostless, CI-covered logic rather than untestable UI glue — the same
/// reason `AssetRequestParsing` lives beside a webview but is not itself one.
enum MinimalZip {
    enum ZipError: Error, CustomStringConvertible, Equatable {
        case eocdNotFound
        case truncated(String)
        case badSignature(String)
        case unsupportedMethod(Int, String)
        case inflateFailed(String)

        var description: String {
            switch self {
            case .eocdNotFound: "no end-of-central-directory record found"
            case .truncated(let where_): "zip data truncated at \(where_)"
            case .badSignature(let where_): "bad zip signature at \(where_)"
            case .unsupportedMethod(let method, let name):
                "unsupported compression method \(method) for \(name)"
            case .inflateFailed(let name): "could not inflate \(name)"
            }
        }
    }

    static func extractAll(_ zipData: Data, to directory: URL) throws {
        let bytes = [UInt8](zipData)
        guard bytes.count >= 22 else { throw ZipError.truncated("end of central directory") }

        func u16(_ offset: Int) -> Int { Int(bytes[offset]) | (Int(bytes[offset + 1]) << 8) }
        func u32(_ offset: Int) -> Int {
            Int(bytes[offset]) | (Int(bytes[offset + 1]) << 8) | (Int(bytes[offset + 2]) << 16)
                | (Int(bytes[offset + 3]) << 24)
        }

        // The EOCD's comment field is variable length, so its signature is found by scanning
        // backward rather than assumed to sit at a fixed offset.
        let searchFloor = max(0, bytes.count - 22 - 65536)
        var eocdOffset = -1
        var index = bytes.count - 22
        while index >= searchFloor {
            if bytes[index] == 0x50, bytes[index + 1] == 0x4B, bytes[index + 2] == 0x05,
                bytes[index + 3] == 0x06
            {
                eocdOffset = index
                break
            }
            index -= 1
        }
        guard eocdOffset >= 0 else { throw ZipError.eocdNotFound }

        let totalEntries = u16(eocdOffset + 10)
        var cursor = u32(eocdOffset + 16)

        for _ in 0..<totalEntries {
            guard cursor + 46 <= bytes.count else { throw ZipError.truncated("central directory") }
            guard bytes[cursor] == 0x50, bytes[cursor + 1] == 0x4B, bytes[cursor + 2] == 0x01,
                bytes[cursor + 3] == 0x02
            else { throw ZipError.badSignature("central directory entry") }

            let method = u16(cursor + 10)
            let compressedSize = u32(cursor + 20)
            let uncompressedSize = u32(cursor + 24)
            let nameLength = u16(cursor + 28)
            let extraLength = u16(cursor + 30)
            let commentLength = u16(cursor + 32)
            let localHeaderOffset = u32(cursor + 42)
            let nameStart = cursor + 46
            guard nameStart + nameLength <= bytes.count else {
                throw ZipError.truncated("central directory filename")
            }
            let name = String(decoding: bytes[nameStart..<(nameStart + nameLength)], as: UTF8.self)

            if !name.hasSuffix("/") {
                let data = try readEntry(
                    bytes: bytes, localHeaderOffset: localHeaderOffset, method: method,
                    compressedSize: compressedSize, uncompressedSize: uncompressedSize, name: name)
                let destination = directory.appending(path: name)
                try FileManager.default.createDirectory(
                    at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
                try data.write(to: destination, options: .atomic)
            }

            cursor = nameStart + nameLength + extraLength + commentLength
        }
    }

    private static func readEntry(
        bytes: [UInt8], localHeaderOffset: Int, method: Int, compressedSize: Int,
        uncompressedSize: Int, name: String
    ) throws -> Data {
        func u16(_ offset: Int) -> Int { Int(bytes[offset]) | (Int(bytes[offset + 1]) << 8) }
        guard localHeaderOffset + 30 <= bytes.count else {
            throw ZipError.truncated("local header for \(name)")
        }
        guard bytes[localHeaderOffset] == 0x50, bytes[localHeaderOffset + 1] == 0x4B,
            bytes[localHeaderOffset + 2] == 0x03, bytes[localHeaderOffset + 3] == 0x04
        else { throw ZipError.badSignature("local header for \(name)") }

        let localNameLength = u16(localHeaderOffset + 26)
        let localExtraLength = u16(localHeaderOffset + 28)
        let dataStart = localHeaderOffset + 30 + localNameLength + localExtraLength
        guard dataStart + compressedSize <= bytes.count else {
            throw ZipError.truncated("data for \(name)")
        }
        let compressed = Data(bytes[dataStart..<(dataStart + compressedSize)])

        switch method {
        case 0: return compressed
        case 8: return try inflate(compressed, uncompressedSize: uncompressedSize, name: name)
        default: throw ZipError.unsupportedMethod(method, name)
        }
    }

    /// Raw DEFLATE (RFC 1951), no zlib or gzip header — what ZIP's method 8 is. Apple's
    /// `COMPRESSION_ZLIB` algorithm, despite the name, decodes exactly this: no header, no
    /// trailing checksum.
    private static func inflate(_ compressed: Data, uncompressedSize: Int, name: String) throws
        -> Data
    {
        guard uncompressedSize > 0 else { return Data() }
        var output = [UInt8](repeating: 0, count: uncompressedSize)
        let compressedBytes = [UInt8](compressed)
        let produced = output.withUnsafeMutableBufferPointer { outBuffer -> Int in
            compressedBytes.withUnsafeBufferPointer { inBuffer in
                guard let outBase = outBuffer.baseAddress, let inBase = inBuffer.baseAddress else {
                    return 0
                }
                return compression_decode_buffer(
                    outBase, uncompressedSize, inBase, inBuffer.count, nil, COMPRESSION_ZLIB)
            }
        }
        guard produced == uncompressedSize else { throw ZipError.inflateFailed(name) }
        return Data(output)
    }
}

#if os(iOS)

    import StoryKit
    import StoryService
    import SwiftUI
    import UIKit
    import WebKit

    #if canImport(Photos)
        import Photos
    #endif

    /// I26. The chain: an export folder I17 already produced, sent to the service I20-22 already
    /// talk to, rendered in the webview I24/I25 already drive.
    ///
    /// **Owns no logic of its own.** Every decision here belongs to a module that is already built
    /// and tested — `NegotiateClient`, `UploadQueue`, `JobPoller`, `AssetSchemeHandler`,
    /// `ReportLoader`. This screen only sequences the calls and shows what they return: real byte
    /// counts, real stage names, `degraded` in the service's own words, never a fabricated
    /// percentage.
    ///
    /// **Auth is the stub, on purpose.** `UnverifiedIdentityHeader` (`ServiceEndpoint.swift`) is
    /// named as a stub, not a design — S06 replaces it. This screen exposes it as a plain text
    /// field for exactly that reason: no sign-in UI belongs here.
    ///
    /// **Two things a real device needs that a simulator does not**, both handled outside this
    /// file: `HostApp/Info.plist` carries a temporary `NSAllowsLocalNetworking` ATS exception and
    /// `NSLocalNetworkUsageDescription` (both commented as temporary, pointing at S06), and the
    /// base URL is a text field — never one hardcoded IP — because a Mac's LAN address changes.
    @available(iOS 17.0, *)
    public struct LoopScreen: View {
        @State private var model = LoopModel()

        public init() {}

        public var body: some View {
            NavigationStack {
                Form {
                    serviceSection
                    folderSection
                    runSection
                    if let bundle = model.reportBundle {
                        reportSection(bundle)
                    }
                    if let message = model.errorMessage {
                        Section("Could not continue") {
                            Text(message).font(.footnote).foregroundStyle(.red)
                                .textSelection(.enabled)
                        }
                    }
                }
                .navigationTitle("Send to the service")
            }
            .task {
                model.restore()
                model.refreshFolders()
            }
            .onChange(of: model.selectedFolder) { _, newFolder in
                model.folderSelectionChanged(to: newFolder)
            }
        }

        private var serviceSection: some View {
            Section {
                TextField("http://192.168.1.23:8000", text: $model.baseURLText)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .onChange(of: model.baseURLText) { _, _ in model.persist() }
                TextField("you@example.com", text: $model.identityText)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .onChange(of: model.identityText) { _, _ in model.persist() }
                HStack {
                    Button("Check service") { Task { await model.checkHealth() } }
                    Spacer()
                    if let reachable = model.serviceReachable {
                        Text(reachable ? "reachable" : "unreachable")
                            .foregroundStyle(reachable ? .green : .red)
                            .font(.footnote)
                    }
                }
            } header: {
                Text("Service")
            } footer: {
                Text(
                    "The Mac's LAN IP, started with --host 0.0.0.0 so a phone can reach it. "
                        + "The identity header is an unverified stub (S06 replaces it) — anything "
                        + "typed here is believed without proof.")
            }
        }

        private var folderSection: some View {
            Section {
                if model.exportedFolders.isEmpty {
                    Text("No exported folders found. Export a trip first.")
                        .foregroundStyle(.secondary)
                } else {
                    Picker("Folder", selection: $model.selectedFolder) {
                        ForEach(model.exportedFolders, id: \.self) { url in
                            Text(url.lastPathComponent).tag(Optional(url))
                        }
                    }
                }
                Button("Refresh") { model.refreshFolders() }
            } header: {
                Text("Exported folder")
            } footer: {
                Text("Pick the folder ExportScreen just wrote to Documents.")
            }
        }

        private var runSection: some View {
            Section {
                Button(model.runButtonTitle) {
                    Task { await model.run() }
                }
                .disabled(model.selectedFolder == nil || model.isRunning)

                phaseView
            } header: {
                Text("Upload and build")
            }
        }

        @ViewBuilder
        private var phaseView: some View {
            switch model.phase {
            case .idle:
                EmptyView()
            case .creatingTrip:
                LabeledContent("Trip", value: "creating…")
            case .negotiating:
                LabeledContent("Negotiate", value: "asking what the service already has…")
            case .uploading(let sent, let total, let bytesToTransfer):
                LabeledContent("Uploading", value: "\(sent) of \(total) assets")
                Text("\(bytesToTransfer) bytes to transfer this negotiation")
                    .font(.footnote).foregroundStyle(.secondary)
            case .preparingSource:
                LabeledContent("Source", value: "service is fetching uploaded media…")
            case .startingBuild:
                LabeledContent("Build", value: "starting…")
            case .polling(let status):
                pollingView(status)
            case .fetchingReport:
                LabeledContent("Report", value: "fetching…")
            case .unzipping:
                LabeledContent("Report", value: "unpacking…")
            case .ready:
                LabeledContent("Report", value: "ready")
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
                    Text("\(done) of \(total) in this stage")
                }
            case .unknown(let reason):
                Text(reason).font(.footnote).foregroundStyle(.secondary)
            }
            switch status.overallPosition {
            case .counted(let done, let total):
                LabeledContent("Pipeline position", value: "\(done) of \(total) stages")
            case .unknown:
                EmptyView()
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

        private func reportSection(_ bundle: ReportBundle) -> some View {
            Section("Report") {
                NavigationLink("View the trip") {
                    LoopReportScreen(bundle: bundle, assetScheme: model.makeAssetScheme())
                }
            }
        }
    }

    /// A full-screen webview over the downloaded, unzipped report — the same `ReportLoader` I24/I25
    /// already test, driven directly (rather than through `ReportWebView`) so this screen can hand
    /// it a real local+remote resolver instead of the pair of `{ _ in nil }` defaults.
    @available(iOS 17.0, *)
    private struct LoopReportScreen: View {
        let bundle: ReportBundle
        let assetScheme: AssetSchemeHandler

        var body: some View {
            LoopReportRepresentable(bundle: bundle, assetScheme: assetScheme)
                .navigationTitle("Trip report")
                .navigationBarTitleDisplayMode(.inline)
        }
    }

    @available(iOS 17.0, *)
    private struct LoopReportRepresentable: UIViewRepresentable {
        let bundle: ReportBundle
        let assetScheme: AssetSchemeHandler

        func makeCoordinator() -> ReportLoader {
            ReportLoader(bundle: bundle, assetScheme: assetScheme) { url in
                UIApplication.shared.open(url)
            }
        }

        func makeUIView(context: Context) -> WKWebView {
            context.coordinator.loadIndex()
            return context.coordinator.webView
        }

        func updateUIView(_ webView: WKWebView, context: Context) {}
    }

    // MARK: - Model

    @available(iOS 17.0, *)
    @MainActor
    @Observable
    final class LoopModel {
        enum Phase: Equatable {
            case idle
            case creatingTrip
            case negotiating
            case uploading(sent: Int, total: Int, bytesToTransfer: Int)
            case preparingSource
            case startingBuild
            case polling(JobStatus)
            case fetchingReport
            case unzipping
            case ready
            case failed(String)
        }

        var baseURLText: String = ""
        var identityText: String = ""
        var exportedFolders: [URL] = []
        var selectedFolder: URL?
        var phase: Phase = .idle
        var reportBundle: ReportBundle?
        var errorMessage: String?
        var serviceReachable: Bool?

        /// Persisted **per exported folder**, so a relaunch resumes rather than restarts (the I26
        /// acceptance criterion) without also gluing a *different* folder onto whichever trip was
        /// last active. `tripID` lets a re-run negotiate into the same trip instead of creating a
        /// second one; `jobID` lets it re-attach to a build already running instead of starting a
        /// new one. Both come from a response this screen already received, never guessed.
        ///
        /// Found the hard way: keying these globally meant picking a *different* folder after a
        /// successful run silently reused the previous folder's trip and, worse, its already-
        /// finished job — so "send" fetched the old report again and looked like nothing happened.
        private var tripID: String?
        private var jobID: String?
        private var runsByFolder: [String: RunRecord] = [:]

        private struct RunRecord: Codable {
            var tripID: String
            var jobID: String?
        }

        var isRunning: Bool {
            switch phase {
            case .idle, .ready, .failed: return false
            default: return true
            }
        }

        var runButtonTitle: String {
            if jobID != nil { return "Resume" }
            if tripID != nil { return "Continue" }
            return "Start"
        }

        // MARK: Persistence

        private enum Keys {
            static let baseURL = "loop.baseURL"
            static let identity = "loop.identity"
            static let folder = "loop.folderPath"
            static let runsByFolder = "loop.runsByFolder"
        }

        func restore() {
            let defaults = UserDefaults.standard
            baseURLText = defaults.string(forKey: Keys.baseURL) ?? ""
            identityText = defaults.string(forKey: Keys.identity) ?? ""
            if let data = defaults.data(forKey: Keys.runsByFolder),
                let decoded = try? JSONDecoder().decode([String: RunRecord].self, from: data)
            {
                runsByFolder = decoded
            }
            if let path = defaults.string(forKey: Keys.folder) {
                selectedFolder = URL(fileURLWithPath: path)
            }
            loadRun(for: selectedFolder)
        }

        func persist() {
            let defaults = UserDefaults.standard
            defaults.set(baseURLText, forKey: Keys.baseURL)
            defaults.set(identityText, forKey: Keys.identity)
            defaults.set(selectedFolder?.path, forKey: Keys.folder)
        }

        /// Called on every folder selection, by the user or by `restore()`: `tripID`/`jobID` must
        /// always mean "this folder's run," never whatever the previous selection left behind.
        func folderSelectionChanged(to folder: URL?) {
            loadRun(for: folder)
        }

        private func loadRun(for folder: URL?) {
            guard let folder, let record = runsByFolder[folder.path] else {
                tripID = nil
                jobID = nil
                return
            }
            tripID = record.tripID
            jobID = record.jobID
        }

        private func saveRun(folder: URL, tripID: String, jobID: String?) {
            runsByFolder[folder.path] = RunRecord(tripID: tripID, jobID: jobID)
            if let data = try? JSONEncoder().encode(runsByFolder) {
                UserDefaults.standard.set(data, forKey: Keys.runsByFolder)
            }
        }

        func refreshFolders() {
            let documents = URL.documentsDirectory
            let found =
                (try? FileManager.default.contentsOfDirectory(
                    at: documents, includingPropertiesForKeys: [.isDirectoryKey]))
                ?? []
            exportedFolders =
                found
                .filter { $0.lastPathComponent.hasPrefix("trip-") }
                .sorted { $0.lastPathComponent > $1.lastPathComponent }
            if selectedFolder == nil { selectedFolder = exportedFolders.first }
        }

        // MARK: Service plumbing

        enum LoopError: Error, CustomStringConvertible {
            case noBaseURL
            case noIdentity
            case noFolder
            case badBaseURL(String)
            case transport(String)
            case reportNotReady(String)

            var description: String {
                switch self {
                case .noBaseURL: "enter the service's base URL first"
                case .noIdentity: "enter an identity (email or phone) first"
                case .noFolder: "pick an exported folder first"
                case .badBaseURL(let text): "\(text) is not a URL"
                case .transport(let detail): detail
                case .reportNotReady(let detail): detail
                }
            }
        }

        private func endpointAndIdentity() throws -> (ServiceEndpoint, UnverifiedIdentityHeader) {
            guard !baseURLText.trimmingCharacters(in: .whitespaces).isEmpty else {
                throw LoopError.noBaseURL
            }
            guard let url = URL(string: baseURLText) else { throw LoopError.badBaseURL(baseURLText) }
            let identity = identityText.trimmingCharacters(in: .whitespaces)
            guard !identity.isEmpty else { throw LoopError.noIdentity }
            return (ServiceEndpoint(baseURL: url), UnverifiedIdentityHeader(identity: identity))
        }

        func checkHealth() async {
            guard let (endpoint, _) = try? endpointAndIdentity() else {
                serviceReachable = nil
                return
            }
            do {
                let (_, response) = try await URLSession.shared.data(from: endpoint.url("/health"))
                serviceReachable = (response as? HTTPURLResponse).map { (200...299).contains($0.statusCode) }
            } catch {
                serviceReachable = false
            }
        }

        // MARK: The loop

        func run() async {
            errorMessage = nil
            do {
                let (endpoint, identity) = try endpointAndIdentity()
                guard let folder = selectedFolder else { throw LoopError.noFolder }

                let client = StoryServiceClient(endpoint: endpoint, identity: identity)

                let resolvedTripID = try await resolveTrip(client: client, folder: folder)
                try await uploadIfNeeded(
                    endpoint: endpoint, identity: identity, client: client, tripID: resolvedTripID,
                    folder: folder)
                try await client.negotiate.prepareSource(tripID: resolvedTripID)

                let resolvedJobID = try await resolveJob(
                    client: client, tripID: resolvedTripID, folder: folder)
                let finalStatus = try await poll(client: client, jobID: resolvedJobID)
                guard finalStatus.state == .succeeded else {
                    throw LoopError.transport(
                        finalStatus.error ?? "the build ended in state \(finalStatus.stateRaw)")
                }
                try await fetchAndUnzipReport(
                    endpoint: endpoint, identity: identity, tripID: resolvedTripID,
                    jobID: resolvedJobID)

                phase = .ready
            } catch {
                let message = "\(error)"
                errorMessage = message
                phase = .failed(message)
            }
        }

        private func resolveTrip(client: StoryServiceClient, folder: URL) async throws -> String {
            if let tripID { return tripID }
            phase = .creatingTrip
            let trip = try await client.negotiate.createTrip(name: folder.lastPathComponent)
            tripID = trip.id
            saveRun(folder: folder, tripID: trip.id, jobID: jobID)
            return trip.id
        }

        private func uploadIfNeeded(
            endpoint: ServiceEndpoint, identity: UnverifiedIdentityHeader, client: StoryServiceClient,
            tripID: String, folder: URL
        ) async throws {
            phase = .negotiating
            let ledgerURL = folder.appending(path: ExportLedgerFile.defaultFileName)
            let ledger = try? ExportLedgerFile.load(from: ledgerURL)
            let scan = try ExportFolder.scan(in: folder, ledger: ledger)
            let negotiation = try await client.negotiate.negotiate(
                tripID: tripID, assets: scan.declarations)
            phase = .uploading(
                sent: 0, total: negotiation.needed.count, bytesToTransfer: negotiation.bytesToTransfer)
            guard !negotiation.needed.isEmpty else { return }

            let stateURL = URL.documentsDirectory.appending(path: "upload-state-\(tripID).json")
            let transport = URLSessionUploadTransport(
                configuration: URLSessionUploadTransport.backgroundConfiguration(
                    identifier: "com.storybook.upload.\(tripID)"))
            let queue = try UploadQueue(stateURL: stateURL, transport: transport)
            await queue.adoptOutstanding()
            await queue.enqueue(negotiation.needed, filesIn: folder)

            let total = negotiation.needed.count
            let pollTask = Task { [weak self] in
                while !Task.isCancelled {
                    let uploaded = await queue.state.filter { $0.state == .uploaded }.count
                    await MainActor.run {
                        self?.phase = .uploading(
                            sent: uploaded, total: total,
                            bytesToTransfer: negotiation.bytesToTransfer)
                    }
                    try? await Task.sleep(for: .milliseconds(500))
                }
            }
            let report = await queue.run()
            pollTask.cancel()
            transport.finishTasksAndInvalidate()

            guard report.isComplete else {
                let reasons =
                    report.failed.map { "\($0.key.prefix(12))…: \($0.value)" }
                    + report.needsRenegotiation.map { "\($0.prefix(12))… needs renegotiation" }
                throw LoopError.transport("upload incomplete: \(reasons.joined(separator: "; "))")
            }
        }

        private func resolveJob(
            client: StoryServiceClient, tripID: String, folder: URL
        ) async throws -> String {
            if let jobID {
                // A build already followed to some point -- ask where it actually stands rather
                // than assume "recorded" means "still relevant." A job that has already reached
                // `succeeded` or `failed` is history: reusing its id here would skip the build
                // entirely and hand back the *old* report, which is exactly the bug where sending
                // a different folder silently kept showing the previous run's result. Only a
                // state this client cannot yet classify (`state == nil`) is treated as possibly
                // still active, matching `JobStatus.state`'s own documented caution about unknown
                // states -- the risk of one extra `job()` call is smaller than the risk of
                // launching a second build for a job that is, in fact, still running.
                let status = try await client.jobs.job(id: jobID)
                let definitelyFinished = status.state == .succeeded || status.state == .failed
                if !definitelyFinished {
                    return jobID
                }
            }
            phase = .startingBuild
            let start = try await client.jobs.startBuild(tripID: tripID)
            jobID = start.job.jobID
            saveRun(folder: folder, tripID: tripID, jobID: start.job.jobID)
            return start.job.jobID
        }

        private func poll(client: StoryServiceClient, jobID: String) async throws -> JobStatus {
            var last: JobStatus?
            for try await reading in client.jobs.follow(jobID: jobID) {
                phase = .polling(reading.status)
                last = reading.status
            }
            guard let last else {
                throw LoopError.transport("no reading came back for job \(jobID)")
            }
            return last
        }

        private func fetchAndUnzipReport(
            endpoint: ServiceEndpoint, identity: UnverifiedIdentityHeader, tripID: String, jobID: String
        ) async throws {
            phase = .fetchingReport
            let metadata = try await fetchReportMetadata(endpoint: endpoint, identity: identity, jobID: jobID)
            guard let downloadURL = URL(string: metadata.bundle.download_url) else {
                throw LoopError.reportNotReady("bundle.download_url did not parse")
            }
            let (zipData, response) = try await URLSession.shared.data(from: downloadURL)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                throw LoopError.transport(
                    "GET report zip: status \((response as? HTTPURLResponse)?.statusCode ?? -1)")
            }

            phase = .unzipping
            let reportRoot = URL.documentsDirectory.appending(path: "report-\(jobID)")
            if FileManager.default.fileExists(atPath: reportRoot.path) {
                try? FileManager.default.removeItem(at: reportRoot)
            }
            try FileManager.default.createDirectory(at: reportRoot, withIntermediateDirectories: true)
            try MinimalZip.extractAll(zipData, to: reportRoot)

            reportBundle = try ReportBundle(root: reportRoot)
        }

        /// Tier 1 (`PHAsset` via the export ledger) and tier 2 (`GET /trips/{id}/media/{relpath}`)
        /// for whatever report is currently loaded. Built fresh each time the report screen opens,
        /// since it needs this run's trip id and folder.
        func makeAssetScheme() -> AssetSchemeHandler {
            var local: @MainActor (AssetRequest) async -> Data? = { _ in nil }
            #if canImport(Photos)
                if let folder = selectedFolder {
                    let ledgerURL = folder.appending(path: ExportLedgerFile.defaultFileName)
                    if let ledger = try? ExportLedgerFile.load(from: ledgerURL) {
                        local = PhotoKitAssetSource(ledger: ledger).resolver()
                    }
                }
            #endif
            var remote: @MainActor (AssetRequest) async -> Data? = { _ in nil }
            if let tripID, let (endpoint, identity) = try? endpointAndIdentity() {
                remote = { request in
                    let relpath = "\(request.kind.rawValue)/\(request.hashPrefix).jpg"
                    return try? await Self.fetchMediaBytes(
                        endpoint: endpoint, identity: identity, tripID: tripID, relpath: relpath)
                }
            }
            return AssetSchemeHandler(local: local, remote: remote)
        }

        // MARK: - Small networking helpers

        /// `ServiceHTTP` (the shared JSON+identity core) is internal to `StoryService`, and neither
        /// `NegotiateClient` nor `JobPoller` cover `GET /jobs/{id}/report` or
        /// `GET /trips/{id}/media/{relpath}` — those routes landed with S05, after I20-22. Rather
        /// than add to a module this task does not own, these two calls are written out here,
        /// reusing the public `ServiceEndpoint`/`ServiceIdentity` types so the wire contract (path
        /// shape, header name) cannot drift from the rest of this target.
        private struct ReportBundleWire: Decodable {
            struct Bundle: Decodable {
                let download_url: String
                let expires_at: String
                let size_bytes: Int
            }
            let job_id: String
            let state: String
            let degraded: Bool?
            let bundle: Bundle
            let media_summary: [String: Int]?
        }

        private func fetchReportMetadata(
            endpoint: ServiceEndpoint, identity: UnverifiedIdentityHeader, jobID: String
        ) async throws -> ReportBundleWire {
            var request = URLRequest(url: endpoint.url("/jobs/\(jobID)/report"))
            request.setValue("application/json", forHTTPHeaderField: "Accept")
            for (name, value) in try await identity.identityHeaders() {
                request.setValue(value, forHTTPHeaderField: name)
            }
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw LoopError.transport("no HTTP response for the report bundle")
            }
            guard (200...299).contains(http.statusCode) else {
                let body = String(data: data.prefix(2000), encoding: .utf8) ?? "<no body>"
                throw LoopError.reportNotReady("GET /jobs/\(jobID)/report: HTTP \(http.statusCode): \(body)")
            }
            return try JSONDecoder().decode(ReportBundleWire.self, from: data)
        }

        private static func fetchMediaBytes(
            endpoint: ServiceEndpoint, identity: UnverifiedIdentityHeader, tripID: String, relpath: String
        ) async throws -> Data {
            var request = URLRequest(url: endpoint.url("/trips/\(tripID)/media/\(relpath)"))
            for (name, value) in try await identity.identityHeaders() {
                request.setValue(value, forHTTPHeaderField: name)
            }
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                throw LoopError.transport(
                    "GET /trips/\(tripID)/media/\(relpath): status "
                        + "\((response as? HTTPURLResponse)?.statusCode ?? -1)")
            }
            return data
        }
    }


#endif
