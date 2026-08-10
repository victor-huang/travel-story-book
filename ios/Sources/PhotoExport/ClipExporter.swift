import AVFoundation
import CoreLocation
import Foundation
import Photos

/// Re-encodes a clip to 1080p **and puts its capture metadata back**, which the preset does not
/// do for you.
///
/// `AVAssetExportSession` at `AVAssetExportPreset1920x1080` writes no QuickTime creation date and
/// no location unless they are set explicitly on the session. Losing them costs the same things
/// losing a still's EXIF costs: days, events, timezone resolution, geocode, home filter, map.
///
/// **What this export deliberately does not carry.** A modern iPhone writes spatial audio as a
/// second `apac` track beside the stereo `mp4a`, plus six `mebx` timed-metadata tracks. The
/// export keeps the video and one stereo audio track and nothing else. That is a choice, not an
/// oversight: mapping every audio stream is what broke 58 of 69 real clips on the Python side,
/// the pipeline reads one waveform for `mean_volume_db` and transcription, and `mebx` carries
/// per-frame GPS that must not leave the phone once `home_filter` has run.
///
/// Handing the asset straight to `AVAssetExportSession` carries the `mebx` tracks through
/// untouched — measured on a real capture, not assumed — so the export runs over a composition
/// holding only the tracks it means to ship. `Output.droppedTrackTags` reports the difference so
/// the loss stays visible.
public enum ClipExporter {
    public struct Output: Sendable, Equatable {
        public let url: URL
        public let filename: String
        public let durationSeconds: Double
        public let audioTrackCount: Int
        /// Tracks present in the source but absent from the export, by their four-character
        /// codec tag. Reported, never silently discarded.
        public let droppedTrackTags: [String]
        public let carriedCreationDate: String?
        public let carriedLocationISO6709: String?
    }

    public enum ExportError: Error {
        case unreadableSource
        case noVideoTrack
        case sessionCreationFailed
        case exportFailed(String)
    }

    /// 1080p, matching the stills' long edge and the source-folder contract.
    public static let preset = AVAssetExportPreset1920x1080

    // MARK: - The core, which needs no photo library

    @discardableResult
    public static func export(
        fileAt sourceURL: URL,
        toDirectory directory: URL,
        filename: String
    ) async throws -> Output {
        let asset = AVURLAsset(url: sourceURL)

        guard try await !asset.loadTracks(withMediaType: .video).isEmpty else {
            throw ExportError.noVideoTrack
        }
        let sourceAudio = try await asset.loadTracks(withMediaType: .audio)
        // Read before exporting: the source is the only place these still exist.
        let sourceTags = try await trackTags(of: asset)

        let creationStamp = try await sourceCreationStamp(of: asset)
        let locationISO6709 = try await sourceLocationISO6709(of: asset)

        let destinationURL = directory.appending(path: filename)
        try? FileManager.default.removeItem(at: destinationURL)

        // Export a composition of *only* the video and first audio track, not the asset itself.
        // Exporting the asset directly carries the six `mebx` timed-metadata tracks straight
        // through -- measured, not assumed -- and those hold per-frame GPS, which must not leave
        // the phone after `home_filter` has run.
        let composition = try await videoAndAudioOnly(from: asset, audioTracks: sourceAudio)

        guard let session = AVAssetExportSession(asset: composition, presetName: preset) else {
            throw ExportError.sessionCreationFailed
        }
        session.metadata = metadataItems(
            creationStamp: creationStamp, locationISO6709: locationISO6709)

        try await run(session, to: destinationURL)

        // The export stamps mvhd/tkhd/mdhd with the moment of export. Copy the source's own
        // header times back over them, so an untimed clip stays untimed instead of claiming it
        // was shot just now — see QuickTimeHeader for the measurement that found this.
        if let times = try? QuickTimeHeader.movieCreationTimes(of: sourceURL) {
            try? QuickTimeHeader.setCreationTimes(
                in: destinationURL, creation: times.creation,
                modification: times.modification)
        }

        // Report against what actually landed, not what was requested.
        let exported = AVURLAsset(url: destinationURL)
        let exportedAudio = try await exported.loadTracks(withMediaType: .audio)
        let exportedTags = try await trackTags(of: exported)
        let duration = try await exported.load(.duration)

        var dropped = sourceTags
        for tag in exportedTags { if let i = dropped.firstIndex(of: tag) { dropped.remove(at: i) } }

        return Output(
            url: destinationURL,
            filename: filename,
            durationSeconds: CMTimeGetSeconds(duration),
            audioTrackCount: exportedAudio.count,
            droppedTrackTags: dropped.sorted(),
            carriedCreationDate: creationStamp,
            carriedLocationISO6709: locationISO6709)
    }

    /// A composition holding the video track and one audio track, and nothing else.
    /// `preferredTransform` is copied so a portrait clip stays portrait — losing it rotates
    /// every phone video on export.
    private static func videoAndAudioOnly(
        from asset: AVURLAsset, audioTracks: [AVAssetTrack]
    ) async throws -> AVMutableComposition {
        let composition = AVMutableComposition()
        let duration = try await asset.load(.duration)
        let range = CMTimeRange(start: .zero, duration: duration)

        guard let sourceVideo = try await asset.loadTracks(withMediaType: .video).first else {
            throw ExportError.noVideoTrack
        }
        guard
            let video = composition.addMutableTrack(
                withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)
        else { throw ExportError.sessionCreationFailed }
        try video.insertTimeRange(range, of: sourceVideo, at: .zero)
        video.preferredTransform = try await sourceVideo.load(.preferredTransform)

        // One audio track: the stereo `mp4a`. The spatial `apac` beside it is deliberately not
        // carried -- mapping every audio stream broke 58 of 69 real clips on the Python side,
        // and the pipeline reads one waveform for mean_volume_db and transcription.
        if let sourceAudio = audioTracks.first,
            let audio = composition.addMutableTrack(
                withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)
        {
            try audio.insertTimeRange(range, of: sourceAudio, at: .zero)
        }

        return composition
    }

    // MARK: - Sources

    @discardableResult
    public static func export(
        _ source: ExportSource,
        toDirectory directory: URL
    ) async throws -> Output {
        switch source {
        case .file(let url):
            return try await export(
                fileAt: url, toDirectory: directory, filename: url.lastPathComponent)
        case .asset(let asset):
            let (resource, resolved) = try await ResourceSelection.resolveAsync(for: asset)
            let staged = try await ResourceSelection.stageToTemporaryFile(resource)
            defer { try? FileManager.default.removeItem(at: staged) }
            return try await export(
                fileAt: staged, toDirectory: directory, filename: resolved.originalFilename)
        }
    }

    // MARK: - Metadata

    /// Both are set explicitly because the preset carries neither. `creationDate` goes in under
    /// the common identifier *and* the QuickTime one: the fixture set already contains a
    /// Photos-shaped `.mov` whose `CreateDate` disagrees with its `Keys:CreationDate`, so
    /// readers plainly consult different places.
    static func metadataItems(creationStamp: String?, locationISO6709: String?) -> [AVMetadataItem] {
        var items: [AVMetadataItem] = []

        if let stamp = creationStamp {
            for identifier in [
                AVMetadataIdentifier.commonIdentifierCreationDate,
                AVMetadataIdentifier.quickTimeMetadataCreationDate,
            ] {
                let item = AVMutableMetadataItem()
                item.identifier = identifier
                item.value = stamp as NSString
                item.dataType = kCMMetadataBaseDataType_UTF8 as String
                items.append(item)
            }
        }

        if let locationISO6709 {
            let item = AVMutableMetadataItem()
            item.identifier = .quickTimeMetadataLocationISO6709
            item.value = locationISO6709 as NSString
            item.dataType = kCMMetadataDataType_QuickTimeMetadataLocation_ISO6709 as String
            items.append(item)
        }

        return items
    }

    /// The creation timestamp **as the source wrote it**, string and all.
    ///
    /// Deliberately not parsed into a `Date` and reformatted. A `Date` is an instant with no
    /// zone, so round-tripping through one turns `2026-08-09T20:01:25+02:00` into
    /// `...18:01:25Z` — the same instant, with the local offset thrown away. That offset is an
    /// input to timezone resolution, which is the highest-risk logic in this project, and this
    /// is the video half of the rule that a still's `OffsetTimeOriginal` must survive.
    private static func sourceCreationStamp(of asset: AVURLAsset) async throws -> String? {
        // Prefer the QuickTime key: it is the one that carries an offset. `.creationDate`
        // resolves to a Date and has already lost it.
        for item in try await asset.load(.metadata)
        where item.identifier == .quickTimeMetadataCreationDate {
            if let string = try await item.load(.stringValue) { return string }
        }
        if let item = try await asset.load(.creationDate) {
            if let string = try await item.load(.stringValue) { return string }
            if let date = try await item.load(.dateValue) {
                let formatter = ISO8601DateFormatter()
                formatter.formatOptions = [.withInternetDateTime]
                return formatter.string(from: date)
            }
        }
        return nil
    }

    private static func sourceLocationISO6709(of asset: AVURLAsset) async throws -> String? {
        for item in try await asset.load(.metadata)
        where item.identifier == .quickTimeMetadataLocationISO6709 {
            if let string = try await item.load(.stringValue) { return string }
        }
        return nil
    }

    /// Four-character codec tags for every track, which is how a dropped `apac` becomes visible.
    private static func trackTags(of asset: AVURLAsset) async throws -> [String] {
        var tags: [String] = []
        for track in try await asset.load(.tracks) {
            let descriptions = try await track.load(.formatDescriptions)
            for description in descriptions {
                let code = CMFormatDescriptionGetMediaSubType(description)
                tags.append(fourCharacterCode(code))
            }
        }
        return tags
    }

    private static func fourCharacterCode(_ code: FourCharCode) -> String {
        let bytes = [
            UInt8((code >> 24) & 0xFF), UInt8((code >> 16) & 0xFF),
            UInt8((code >> 8) & 0xFF), UInt8(code & 0xFF),
        ]
        return String(bytes: bytes, encoding: .ascii)?
            .trimmingCharacters(in: .whitespaces) ?? "????"
    }

    // MARK: - Plumbing

    private static func run(_ session: AVAssetExportSession, to url: URL) async throws {
        if #available(iOS 18.0, macOS 15.0, *) {
            try await session.export(to: url, as: .mov)
            return
        }

        session.outputURL = url
        session.outputFileType = .mov
        // AVAssetExportSession predates Sendable; the box is confined to this function, which
        // awaits completion before reading anything back.
        struct Unchecked<T>: @unchecked Sendable { let value: T }
        let boxed = Unchecked(value: session)
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            boxed.value.exportAsynchronously { continuation.resume() }
        }
        guard session.status == .completed else {
            throw ExportError.exportFailed(
                session.error?.localizedDescription ?? "status \(session.status.rawValue)")
        }
    }
}
