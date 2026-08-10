import AVFoundation
import Foundation
import Testing

@testable import PhotoExport

/// I12's acceptance criterion: creation date and location survive a device-captured clip, and
/// the exported bytes are verified to be a video rather than merely present under a `.mov` name.
///
/// These run in CI — AVFoundation needs no photo library (D12). Re-encoding is slow, so the
/// small synthetic clip carries the general behaviour and the 18 MB device capture is reserved
/// for what only it can prove.
@Suite(.serialized)
struct ClipExporterTests {
    private func tempDir() throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "clip-export-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private var spatialFixture: URL {
        LibraryFixtures.url(forFixture: "iphone_spatial_audio.mov")
    }
    private var smallClip: URL {
        LibraryFixtures.url(forFixture: "clip_speech.mov")
    }

    // MARK: - Metadata, on the device capture

    /// The offset is the point. Round-tripping the stamp through a `Date` would turn
    /// `20:01:25+02:00` into `18:01:25Z` — the same instant with the local offset discarded —
    /// and the offset is an input to timezone resolution.
    @Test func creationDateSurvivesWithItsUTCOffset() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try await ClipExporter.export(
            fileAt: spatialFixture, toDirectory: dir, filename: "out.mov")

        let stamp = try #require(out.carriedCreationDate)
        #expect(stamp.contains("20:01:25"))
        #expect(
            stamp.contains("+0200") || stamp.contains("+02:00"),
            "offset lost, stamp was \(stamp)")

        // And it is actually in the file, not merely in the return value.
        let exported = AVURLAsset(url: out.url)
        let written = try await exported.load(.metadata)
            .first { $0.identifier == .quickTimeMetadataCreationDate }
        #expect(written != nil, "no QuickTime creation date in the exported file")
    }

    @Test func locationSurvives() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try await ClipExporter.export(
            fileAt: spatialFixture, toDirectory: dir, filename: "out.mov")

        let iso = try #require(out.carriedLocationISO6709)
        #expect(iso.hasPrefix("+47.7949"))

        let exported = AVURLAsset(url: out.url)
        let written = try await exported.load(.metadata)
            .first { $0.identifier == .quickTimeMetadataLocationISO6709 }
        #expect(written != nil, "no location in the exported file")
    }

    // MARK: - What the export carries, and what it does not

    /// The spatial `apac` track is deliberately not carried: mapping every audio stream broke
    /// 58 of 69 real clips on the Python side.
    @Test func spatialAudioTrackIsDropped() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try await ClipExporter.export(
            fileAt: spatialFixture, toDirectory: dir, filename: "out.mov")

        #expect(out.audioTrackCount == 1)
        #expect(out.droppedTrackTags.contains("apac"))
        // Pair the measurement with a control: the source really did have two audio tracks, so
        // "one track out" is a decision and not an artifact of the fixture.
        let source = AVURLAsset(url: spatialFixture)
        #expect(try await source.loadTracks(withMediaType: .audio).count == 2)
    }

    /// `mebx` carries per-frame GPS. Handing the asset straight to `AVAssetExportSession` copies
    /// those tracks through untouched — this test is what keeps the composition step in place.
    @Test func timedMetadataTracksAreDropped() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try await ClipExporter.export(
            fileAt: spatialFixture, toDirectory: dir, filename: "out.mov")

        #expect(out.droppedTrackTags.contains("mebx"))
        let exported = AVURLAsset(url: out.url)
        let kinds = try await exported.load(.tracks).map { $0.mediaType }
        #expect(!kinds.contains(.metadata), "a timed-metadata track reached the export")
        #expect(Set(kinds) == Set([.video, .audio]))
    }

    /// An export that quietly kept everything would still pass the metadata tests. The report of
    /// what was dropped is the thing that makes the loss visible.
    @Test func droppedTracksAreReported() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try await ClipExporter.export(
            fileAt: spatialFixture, toDirectory: dir, filename: "out.mov")
        #expect(!out.droppedTrackTags.isEmpty)
    }

    // MARK: - Shape

    @Test func portraitClipStaysPortrait() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try await ClipExporter.export(
            fileAt: spatialFixture, toDirectory: dir, filename: "out.mov")

        let track = try #require(
            try await AVURLAsset(url: out.url).loadTracks(withMediaType: .video).first)
        let size = try await track.load(.naturalSize)
            .applying(try await track.load(.preferredTransform))
        // Losing preferredTransform rotates every phone video; the assertion is on the
        // displayed shape, not the stored one.
        #expect(abs(size.height) > abs(size.width), "clip came out landscape")
    }

    @Test func longEdgeIsAtMost1920AndShortEdge1080() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try await ClipExporter.export(
            fileAt: spatialFixture, toDirectory: dir, filename: "out.mov")

        let track = try #require(
            try await AVURLAsset(url: out.url).loadTracks(withMediaType: .video).first)
        let size = try await track.load(.naturalSize)
        #expect(max(size.width, size.height) <= 1920)
        #expect(min(size.width, size.height) <= 1080)
    }

    @Test func durationIsPreserved() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try await ClipExporter.export(
            fileAt: spatialFixture, toDirectory: dir, filename: "out.mov")
        #expect(abs(out.durationSeconds - 11.54) < 0.1)
    }

    // MARK: - Identity, and the cheap cases

    /// P06 found nine assets declared `kind: "video"` whose exported files were JPEGs under
    /// `.mov` names, past a schema check and 87 passing tests. Verify the bytes.
    @Test func exportedFileIsActuallyAVideo() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try await ClipExporter.export(
            fileAt: smallClip, toDirectory: dir, filename: "out.mov")

        let asset = AVURLAsset(url: out.url)
        #expect(try await !asset.loadTracks(withMediaType: .video).isEmpty)
        #expect(try await asset.load(.isPlayable))
        // A JPEG under a .mov name is bigger than zero and opens as nothing.
        let size = try FileManager.default.attributesOfItem(atPath: out.url.path)[.size] as? Int
        #expect((size ?? 0) > 1000)
    }

    @Test func keepsTheOriginalFilename() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try await ClipExporter.export(
            .file(smallClip), toDirectory: dir)
        #expect(out.filename == "clip_speech.mov")
    }

    /// A clip with no audio must export rather than throw — the pipeline degrades on missing
    /// data, so the exporter must not be the thing that aborts.
    @Test func silentClipStillExports() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try await ClipExporter.export(
            fileAt: LibraryFixtures.url(forFixture: "clip_silent.mp4"),
            toDirectory: dir, filename: "out.mov")
        #expect(FileManager.default.fileExists(atPath: out.url.path))
        #expect(out.durationSeconds > 0)
    }

    @Test func stillImageSourceThrowsRatherThanWritingAnEmptyClip() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        await #expect(throws: (any Error).self) {
            try await ClipExporter.export(
                fileAt: LibraryFixtures.url(forFixture: "jpeg_no_exif.jpg"),
                toDirectory: dir, filename: "out.mov")
        }
    }
}
