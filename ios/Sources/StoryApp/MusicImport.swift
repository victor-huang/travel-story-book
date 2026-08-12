import AVFoundation
import Foundation
import StoryKit
import StoryService

/// I31. Bring in a music track for a reel, from Files or iCloud Drive — **never** a picker over
/// the Apple Music library.
///
/// Apple Music tracks (subscription streams and most purchases since the FairPlay era) are
/// DRM-protected, and AVFoundation cannot export or mix protected audio into anything — a reel
/// built with one would render silently, with no sound and no error, and the traveller would only
/// find out watching the result. So this screen offers `UIDocumentPickerViewController` /
/// `.fileImporter` over the filesystem, where a picked file is a file — bytes this app can read,
/// hash, and hand to the pipeline — never `MPMediaPickerController`, which hands out library
/// items, not files, specifically because most of them cannot leave the Music app's own sandboxed
/// playback path.
///
/// **The DRM check runs before any hash, negotiate or upload call** — refusing after the bytes
/// are already on the service would be the exact "discovered when the reel comes back silent"
/// failure the task exists to prevent.
///
/// **No new upload mechanism.** Once a track passes the check, ``MusicImporter`` hands it to the
/// same `NegotiateClient`/`UploadQueue` pair every other asset goes through (I20/I21) — a music
/// file is, structurally, just another hash-addressed asset of this trip, per S07's own log entry
/// ("there is no separate upload path for music").

// MARK: - DRM detection

/// Hostless on purpose — AVFoundation is available on macOS too, so this runs under `swift test`
/// on the CI host with no simulator, unlike the SwiftUI picker below it.
public enum DRMCheck {
    public enum Verdict: Sendable, Equatable {
        case clear
        /// Refused. `reason` is shown to the traveller verbatim — never paraphrased into "this
        /// file didn't work."
        case protectedContent(reason: String)
        /// Not necessarily DRM — could be a corrupt file, an unsupported container, or a video
        /// with no audio track — but equally unusable, and worth telling apart from a DRM refusal
        /// in the message, since the fix ("get a different file") differs from the fix ("get a
        /// DRM-free file").
        case unreadable(reason: String)
    }

    /// `.m4p` is the extension iTunes used for FairPlay-protected AAC purchases before Apple moved
    /// the store to DRM-free files in 2009. Checked before the file is even opened: a fast, exact
    /// refusal for the one case a filename alone can prove.
    ///
    /// **What this cannot catch:** a DRM-free file renamed to `.m4p` (refused anyway — a false
    /// positive an honest user can work around by renaming it back), a protected file saved under
    /// an ordinary `.m4a`/`.aac` extension (falls through to the AVFoundation check below), and
    /// any DRM scheme AVFoundation itself does not recognise as "protected content" — this check
    /// only knows what FairPlay-flavoured containers AVFoundation flags, not DRM in general.
    public static let legacyProtectedExtensions: Set<String> = ["m4p"]

    /// Checks, in order: the legacy extension, then whatever AVFoundation itself can tell about
    /// the file without fully decoding it.
    ///
    /// **The honest limits of this check**, stated rather than implied by a confident boolean:
    /// `AVURLAsset.load(.hasProtectedContent)` reports FairPlay and the other DRM schemes
    /// AVFoundation itself understands. It is *not* a general "is this legally mine" check — a
    /// stream ripped from a protected source with third-party tools would read as unprotected
    /// bytes to AVFoundation, because the protection was already removed before this ever sees the
    /// file. This check can only refuse what AVFoundation itself would also refuse to export.
    public static func check(url: URL) async -> Verdict {
        if legacyProtectedExtensions.contains(url.pathExtension.lowercased()) {
            return .protectedContent(
                reason:
                    "\(url.lastPathComponent) has the .m4p extension iTunes used for "
                    + "FairPlay-protected purchases. AVFoundation cannot export or mix "
                    + "DRM-protected audio into a reel.")
        }

        let asset = AVURLAsset(url: url)
        let protected: Bool
        do {
            protected = try await asset.load(.hasProtectedContent)
        } catch {
            return .unreadable(
                reason: "could not inspect \(url.lastPathComponent) for protection: \(error)")
        }
        if protected {
            return .protectedContent(
                reason:
                    "AVFoundation reports \(url.lastPathComponent) as protected content "
                    + "(FairPlay or another DRM scheme). It cannot be exported or mixed into a "
                    + "reel.")
        }

        do {
            let audioTracks = try await asset.loadTracks(withMediaType: .audio)
            guard !audioTracks.isEmpty else {
                return .unreadable(
                    reason: "\(url.lastPathComponent) has no audio track AVFoundation can read.")
            }
        } catch {
            return .unreadable(reason: "could not read \(url.lastPathComponent): \(error)")
        }
        return .clear
    }
}

// MARK: - Import: hash, negotiate, upload

/// Copies a picked file into a working directory, hashes it, and pushes it through the same
/// ingest path any exported photo or clip uses. Hostless: no UIKit, so the whole chain except the
/// system file picker itself is covered by `swift test` on the CI host.
public struct MusicImporter: Sendable {
    public enum ImportError: Error, Sendable, Equatable, CustomStringConvertible {
        case protectedContent(String)
        case unreadable(String)
        case copyFailed(String)
        case uploadFailed(String)

        public var description: String {
            switch self {
            case .protectedContent(let reason): reason
            case .unreadable(let reason): reason
            case .copyFailed(let reason): "could not import this file: \(reason)"
            case .uploadFailed(let reason): "upload failed: \(reason)"
            }
        }
    }

    public struct Imported: Sendable, Equatable {
        public let hash: String
        public let filename: String
        public let size: Int
    }

    private let client: StoryServiceClient
    private let tripID: String
    private let workingDirectory: URL
    private let makeTransport: @Sendable () -> any UploadTransport

    public init(
        client: StoryServiceClient,
        tripID: String,
        workingDirectory: URL,
        makeTransport: @escaping @Sendable () -> any UploadTransport = {
            URLSessionUploadTransport(configuration: .default)
        }
    ) {
        self.client = client
        self.tripID = tripID
        self.workingDirectory = workingDirectory
        self.makeTransport = makeTransport
    }

    /// The whole chain: DRM check (first, and the only step that can refuse), copy, hash,
    /// negotiate, upload. Any failure after the DRM check is a transport or service failure, not a
    /// refusal — the two read differently to a caller because the fix is different.
    public func importTrack(from pickedURL: URL) async throws -> Imported {
        let verdict = await DRMCheck.check(url: pickedURL)
        switch verdict {
        case .protectedContent(let reason): throw ImportError.protectedContent(reason)
        case .unreadable(let reason): throw ImportError.unreadable(reason)
        case .clear: break
        }

        let filename = pickedURL.lastPathComponent
        let fm = FileManager.default
        do {
            try fm.createDirectory(at: workingDirectory, withIntermediateDirectories: true)
        } catch {
            throw ImportError.copyFailed("\(error)")
        }
        let destination = workingDirectory.appending(path: filename)

        // `.fileImporter`/`UIDocumentPickerViewController` hand out security-scoped URLs; bracket
        // access here so this type works the same whether or not the caller already did.
        let scoped = pickedURL.startAccessingSecurityScopedResource()
        defer { if scoped { pickedURL.stopAccessingSecurityScopedResource() } }

        do {
            if fm.fileExists(atPath: destination.path) {
                try fm.removeItem(at: destination)
            }
            try fm.copyItem(at: pickedURL, to: destination)
        } catch {
            throw ImportError.copyFailed("\(error)")
        }

        let attributes: [FileAttributeKey: Any]
        do {
            attributes = try fm.attributesOfItem(atPath: destination.path)
        } catch {
            throw ImportError.copyFailed("\(error)")
        }
        let size = (attributes[.size] as? NSNumber)?.intValue ?? 0
        let hash: String
        do {
            hash = try ContentHash.hex(of: destination)
        } catch {
            throw ImportError.copyFailed("\(error)")
        }
        let declaration: AssetDeclaration
        do {
            declaration = try AssetDeclaration(hash: hash, filename: filename, size: size)
        } catch {
            throw ImportError.copyFailed("\(error)")
        }

        let negotiation: Negotiation
        do {
            negotiation = try await client.negotiate.negotiate(
                tripID: tripID, assets: [declaration])
        } catch {
            throw ImportError.uploadFailed("negotiate: \(error)")
        }

        guard let ticket = negotiation.needed.first(where: { $0.hash == hash }) else {
            // Already declared under this hash -- re-importing the same track, or one already
            // uploaded for a previous reel on this trip. Nothing to upload; still a success.
            return Imported(hash: hash, filename: filename, size: size)
        }

        let stateURL = workingDirectory.appending(path: ".music-upload-state-\(hash.prefix(16)).json")
        let queue: UploadQueue
        do {
            queue = try UploadQueue(stateURL: stateURL, transport: makeTransport())
        } catch {
            throw ImportError.uploadFailed("\(error)")
        }
        await queue.enqueue([ticket], filesIn: workingDirectory)
        let report = await queue.run()
        guard report.isComplete else {
            let reasons =
                report.failed.values.map { $0 }
                + report.needsRenegotiation.map { "\($0.prefix(12))… needs renegotiation" }
            throw ImportError.uploadFailed(reasons.joined(separator: "; "))
        }
        return Imported(hash: hash, filename: filename, size: size)
    }
}

#if os(iOS)

    import SwiftUI
    import UniformTypeIdentifiers

    /// The screen-facing half: a button, the system file picker, and real state — never a
    /// fabricated "importing…" that outlives the actual work.
    @available(iOS 17.0, *)
    @MainActor
    @Observable
    public final class MusicImportModel {
        public enum State: Equatable {
            case idle
            case checking
            case uploading
            case imported(hash: String, filename: String)
            /// Refused before upload — a DRM or unreadable verdict. Distinct from `.failed`
            /// because the traveller's next move differs: pick a different file, not retry.
            case refused(String)
            case failed(String)
        }

        public var state: State = .idle

        private let makeImporter: @Sendable () -> MusicImporter

        public init(client: StoryServiceClient, tripID: String, workingDirectory: URL) {
            self.makeImporter = {
                MusicImporter(client: client, tripID: tripID, workingDirectory: workingDirectory)
            }
        }

        /// For tests and previews: inject the importer directly rather than the pieces it is built
        /// from.
        init(makeImporter: @escaping @Sendable () -> MusicImporter) {
            self.makeImporter = makeImporter
        }

        public var musicHash: String? {
            if case .imported(let hash, _) = state { return hash }
            return nil
        }

        public var isBusy: Bool {
            state == .checking || state == .uploading
        }

        public func `import`(from url: URL) async {
            state = .checking
            do {
                let imported = try await makeImporter().importTrack(from: url)
                state = .imported(hash: imported.hash, filename: imported.filename)
            } catch let error as MusicImporter.ImportError {
                switch error {
                case .protectedContent(let reason), .unreadable(let reason):
                    state = .refused(reason)
                case .copyFailed(let reason), .uploadFailed(let reason):
                    state = .failed(reason)
                }
            } catch {
                state = .failed("\(error)")
            }
        }

        public func clear() {
            state = .idle
        }
    }

    /// A `Form` section: pick a track, see exactly why a refused one was refused, or skip music
    /// entirely (the reel renders with none — `music_hash` is optional on `POST /trips/{id}/reel`).
    @available(iOS 17.0, *)
    public struct MusicImportSection: View {
        var model: MusicImportModel
        @State private var showingPicker = false

        public init(model: MusicImportModel) {
            self.model = model
        }

        public var body: some View {
            Section {
                Button(buttonTitle) { showingPicker = true }
                    .disabled(model.isBusy)
                stateView
                if model.musicHash != nil {
                    Button("Remove music", role: .destructive) { model.clear() }
                }
            } header: {
                Text("Music")
            } footer: {
                Text(
                    "From Files or iCloud Drive only — never the Apple Music library. Those "
                        + "tracks are DRM-protected and cannot be mixed into a reel. Skip this to "
                        + "render without music.")
            }
            .fileImporter(
                isPresented: $showingPicker, allowedContentTypes: [.audio],
                allowsMultipleSelection: false
            ) { result in
                switch result {
                case .success(let urls):
                    guard let url = urls.first else { return }
                    Task { await model.import(from: url) }
                case .failure(let error):
                    model.state = .failed("\(error)")
                }
            }
        }

        private var buttonTitle: String {
            switch model.state {
            case .imported: "Choose a different track"
            default: "Choose a track"
            }
        }

        @ViewBuilder
        private var stateView: some View {
            switch model.state {
            case .idle:
                EmptyView()
            case .checking:
                LabeledContent("Music", value: "checking the file…")
            case .uploading:
                LabeledContent("Music", value: "uploading…")
            case .imported(_, let filename):
                LabeledContent("Music", value: filename)
            case .refused(let reason):
                Text(reason).font(.footnote).foregroundStyle(.red)
            case .failed(let reason):
                Text(reason).font(.footnote).foregroundStyle(.red)
            }
        }
    }

#endif
