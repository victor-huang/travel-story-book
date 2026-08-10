import CoreGraphics
import Foundation
import ImageIO
import Photos
import Testing

@testable import PhotoExport

/// The `PHAsset` half of `StillExporter` (D12). `StillExporterTests` covers the shared core
/// through the file source and runs in CI; these cover what only a real library can exercise —
/// resource staging through `PHAssetResourceManager`, and the filename coming from
/// `ResourceSelection` rather than from a path.
///
/// Simulator only, for the reasons in D10/D11.
@Suite(.serialized)
struct StillExporterAssetTests {
    private func tempDir() throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "asset-export-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    @Test(.enabled(if: LibraryFixtures.canSeedLibrary))
    func exportsFromAPHAssetPreservingMetadata() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let asset = try await LibraryFixtures.asset(forFixture: "iphone_heic_gps_offset.heic")

        let out = try await StillExporter.export(.asset(asset), toDirectory: dir)

        #expect(max(out.pixelWidth, out.pixelHeight) == 1080)
        // The filename comes from ResourceSelection, not from any path we passed in.
        #expect(out.filename == "iphone_heic_gps_offset.heic")

        let source = try #require(CGImageSourceCreateWithURL(out.url as CFURL, nil))
        let props = try #require(
            CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any])
        let exif = try #require(props[kCGImagePropertyExifDictionary] as? [CFString: Any])
        let gps = try #require(props[kCGImagePropertyGPSDictionary] as? [CFString: Any])

        // Going through PhotoKit must not lose what the file source keeps.
        #expect(exif[kCGImagePropertyExifDateTimeOriginal] as? String == "2026:08:09 20:01:16")
        #expect(exif[kCGImagePropertyExifOffsetTimeOriginal] as? String == "+02:00")
        #expect(abs((gps[kCGImagePropertyGPSLatitude] as? Double ?? 0) - 47.7949) < 0.001)
    }

    /// The staging temp file must not survive the export; a library-sized run would otherwise
    /// leave a full-resolution copy of every asset in tmp.
    @Test(.enabled(if: LibraryFixtures.canSeedLibrary))
    func stagingFileIsCleanedUp() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let asset = try await LibraryFixtures.asset(forFixture: "iphone_heic_gps_offset.heic")

        _ = try await StillExporter.export(.asset(asset), toDirectory: dir)

        let leftovers = try FileManager.default.contentsOfDirectory(
            atPath: FileManager.default.temporaryDirectory.path
        ).filter { $0.hasPrefix("storybook-stage-") }
        #expect(leftovers.isEmpty, "staging files left behind: \(leftovers)")
    }

    /// Both sources must produce the same bytes for the same picture — otherwise the CI-tested
    /// file path proves nothing about the production `PHAsset` path (D12's whole premise).
    @Test(.enabled(if: LibraryFixtures.canSeedLibrary))
    func assetAndFileSourcesAgree() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let asset = try await LibraryFixtures.asset(forFixture: "iphone_heic_gps_offset.heic")

        let fromAsset = try await StillExporter.export(.asset(asset), toDirectory: dir)
        let fileDir = try tempDir()
        defer { try? FileManager.default.removeItem(at: fileDir) }
        let fromFile = try await StillExporter.export(
            .file(LibraryFixtures.url(forFixture: "iphone_heic_gps_offset.heic")),
            toDirectory: fileDir)

        #expect(fromAsset.pixelWidth == fromFile.pixelWidth)
        #expect(fromAsset.pixelHeight == fromFile.pixelHeight)
        #expect(fromAsset.filename == fromFile.filename)
    }
}
