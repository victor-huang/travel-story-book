import Foundation
import Photos
import StoryKit

/// Writes the source folder the CLI already accepts: 1080px stills, 1080p clips, original
/// filenames, a sidecar beside each item, and an `ExportLedger` recording which `PHAsset`
/// produced which content hash.
///
/// **Resumability is the requirement here, not speed.** A phone export happens across many short
/// foreground sessions: `BGProcessingTask` is opportunistic with no guarantee, and jetsam kills
/// on memory. The pipeline's own resumability does not help — that is the laptop's half. So the
/// ledger is written after **every** item, and an interrupt costs at most one item's work.
///
/// The ledger, not the presence of a file, is what "already done" means. A run killed mid-write
/// leaves a truncated file that is *not* in the ledger, so the next run redoes it; treating file
/// existence as completion would ship the truncation.
public actor FolderWriter {
    public struct Summary: Sendable, Equatable {
        public var written: Int = 0
        /// Already in the ledger from an earlier session — the resume path.
        public var skippedAlreadyExported: Int = 0
        public var excludedNearHome: Int = 0
        /// Excluded because location is unknown and a home is configured. Counted apart from
        /// `excludedNearHome` because these are ambiguous rather than private.
        public var excludedUnknownLocation: Int = 0
        /// Per-item failures. One bad file costs only itself.
        public var failed: [String] = []

        public var attempted: Int { written + skippedAlreadyExported }
    }

    public enum WriterError: Error {
        case destinationNotWritable(String)
    }

    private let destination: URL
    private let ledgerURL: URL
    private let home: HomeFilter.Home?
    private var ledger: ExportLedger

    /// - Parameters:
    ///   - destination: the source folder handed to `story-book build`.
    ///   - ledgerURL: defaults to `export_ledger.json` inside `destination`. It is not media, so
    ///     the scanner ignores it, and keeping it beside the folder means a resumed export finds
    ///     it without extra state.
    ///   - home: when non-nil, `HomeFilter` runs before anything is written.
    public init(
        destination: URL,
        home: HomeFilter.Home? = nil,
        ledgerURL: URL? = nil
    ) throws {
        self.destination = destination
        self.home = home
        self.ledgerURL =
            ledgerURL ?? destination.appending(path: ExportLedgerFile.defaultFileName)

        try FileManager.default.createDirectory(
            at: destination, withIntermediateDirectories: true)
        guard FileManager.default.isWritableFile(atPath: destination.path) else {
            throw WriterError.destinationNotWritable(destination.path)
        }
        self.ledger = try ExportLedgerFile.load(from: self.ledgerURL)
    }

    public var ledgerSnapshot: ExportLedger { ledger }

    /// Export every source, skipping what a previous session already finished.
    ///
    /// `onProgress` reports real counts, never a fabricated percentage.
    @discardableResult
    public func export(
        _ sources: [ExportSource],
        onProgress: (@Sendable (Int, Int) -> Void)? = nil
    ) async -> Summary {
        var summary = Summary()

        for (index, source) in sources.enumerated() {
            defer { onProgress?(index + 1, sources.count) }

            // Privacy first, before a single byte is written or hashed. This is the whole
            // reason the filter is not a pipeline stage on this side.
            switch exclusion(for: source) {
            case .nearHome:
                summary.excludedNearHome += 1
                continue
            case .unknownLocation:
                summary.excludedUnknownLocation += 1
                continue
            case .none:
                break
            }

            if let identifier = localIdentifier(of: source),
                ledger.entry(forLocalIdentifier: identifier) != nil
            {
                summary.skippedAlreadyExported += 1
                continue
            }

            do {
                try await write(source)
                summary.written += 1
            } catch {
                // Batching trades away fault isolation -- pay it back. One unreadable asset
                // must not cost the other 800.
                summary.failed.append(describe(source))
            }
        }

        return summary
    }

    // MARK: - One item

    private func write(_ source: ExportSource) async throws {
        let kind = try await mediaKind(of: source)
        let output: (url: URL, filename: String)

        switch kind {
        case .still:
            let result = try await StillExporter.export(source, toDirectory: destination)
            output = (result.url, result.filename)
        case .video:
            let result = try await ClipExporter.export(source, toDirectory: destination)
            output = (result.url, result.filename)
        }

        // The hash is of the *exported* bytes, which is why only the phone at export time can
        // record the PHAsset -> hash mapping (D6). Chunked: a 400 MB clip is not held in memory.
        let hash = try ContentHash.hex(of: output.url)

        try writeSidecar(for: source, filename: output.filename, contentHash: hash)

        if let identifier = localIdentifier(of: source) {
            ledger.record(
                .init(
                    localIdentifier: identifier,
                    contentHash: hash,
                    originalFilename: output.filename))
            // After every item, not at the end. An interrupt costs one item.
            try ExportLedgerFile.save(ledger, to: ledgerURL)
        }
    }

    private func writeSidecar(
        for source: ExportSource, filename: String, contentHash: String
    ) throws {
        let creation: Sidecar.Creation
        let location: Sidecar.Location

        switch source {
        case .file:
            // A file on disk carries no PhotoKit truth. Saying so is the point: the pipeline
            // then falls back to what the file itself holds, rather than trusting a value this
            // side invented.
            creation = .unavailable
            location = .unavailable
        case .asset(let asset):
            if let date = asset.creationDate {
                let formatter = ISO8601DateFormatter()
                formatter.formatOptions = [.withInternetDateTime]
                formatter.timeZone = TimeZone(secondsFromGMT: 0)
                creation = .init(utc: formatter.string(from: date), source: .photokitAsset)
            } else {
                creation = .unavailable
            }
            if let coordinate = asset.location?.coordinate {
                location = .init(
                    lat: coordinate.latitude, lon: coordinate.longitude, source: .photokitAsset)
            } else {
                location = .unavailable
            }
        }

        let sidecar = Sidecar(
            contentHash: contentHash,
            originalFilename: filename,
            creation: creation,
            location: location)
        let url = destination.appending(path: Sidecar.fileName(forMediaFilename: filename))
        try sidecar.encoded().write(to: url, options: .atomic)
    }

    // MARK: - Classification

    private enum Exclusion { case none, nearHome, unknownLocation }

    private func exclusion(for source: ExportSource) -> Exclusion {
        guard let home else { return .none }
        switch source {
        case .file:
            // A file source has no PhotoKit coordinate to test. Its EXIF may well carry one, but
            // reading it here would duplicate the pipeline's own extraction and risk
            // disagreeing with it; the file path exists for tests and the parity harness, where
            // no home is configured.
            return .none
        case .asset(let asset):
            guard let coordinate = asset.location?.coordinate else { return .unknownLocation }
            return HomeFilter.isWithinHomeRadius(
                latitude: coordinate.latitude, longitude: coordinate.longitude, home: home)
                ? .nearHome : .none
        }
    }

    private func localIdentifier(of source: ExportSource) -> String? {
        switch source {
        case .file(let url): return url.path
        case .asset(let asset): return asset.localIdentifier
        }
    }

    private func describe(_ source: ExportSource) -> String {
        switch source {
        case .file(let url): return url.lastPathComponent
        case .asset(let asset): return asset.localIdentifier
        }
    }

    private func mediaKind(of source: ExportSource) async throws -> ResolvedResource.Kind {
        switch source {
        case .file(let url):
            let videoExtensions: Set<String> = ["mov", "mp4", "m4v", "avi"]
            return videoExtensions.contains(url.pathExtension.lowercased()) ? .video : .still
        case .asset(let asset):
            return asset.mediaType == .video ? .video : .still
        }
    }
}
