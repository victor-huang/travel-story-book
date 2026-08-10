import CoreGraphics
import Foundation
import ImageIO
import Testing

@testable import PhotoExport

/// I11's acceptance criterion: every field the pipeline consumes survives a 1080px export of
/// the HEIC+GPS+offset fixture. These run in CI — the file source needs no photo library (D12).
struct StillExporterTests {
    private func tempDir() throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "still-export-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func properties(of url: URL) throws -> [CFString: Any] {
        let source = try #require(CGImageSourceCreateWithURL(url as CFURL, nil))
        return try #require(
            CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any])
    }

    /// The real iPhone capture, which is the one that matters: a synthetic fixture cannot prove
    /// a real HEIC container survives.
    private var heicFixture: URL {
        LibraryFixtures.url(forFixture: "iphone_heic_gps_offset.heic")
    }

    @Test func downscalesToTheRequestedLongEdge() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try StillExporter.export(
            fileAt: heicFixture, toDirectory: dir, filename: "iphone_heic_gps_offset.heic")
        #expect(max(out.pixelWidth, out.pixelHeight) == 1080)
        // A control that must differ: the source is 4032x3024, so an unchanged size would mean
        // the downscale silently did nothing.
        #expect(out.pixelWidth < 4032)
    }

    @Test func keepsTheOriginalFilename() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try StillExporter.export(
            fileAt: heicFixture, toDirectory: dir, filename: "iphone_heic_gps_offset.heic")
        #expect(out.url.lastPathComponent == "iphone_heic_gps_offset.heic")
        #expect(FileManager.default.fileExists(atPath: out.url.path))
    }

    /// The core of I11. Each of these has a named consumer in the pipeline; losing any one
    /// silently degrades the story rather than failing loudly.
    @Test func captureTimeAndOffsetSurvive() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try StillExporter.export(
            fileAt: heicFixture, toDirectory: dir, filename: "out.heic")

        let props = try properties(of: out.url)
        let exif = try #require(props[kCGImagePropertyExifDictionary] as? [CFString: Any])
        #expect(exif[kCGImagePropertyExifDateTimeOriginal] as? String == "2026:08:09 20:01:16")
        #expect(exif[kCGImagePropertyExifOffsetTimeOriginal] as? String == "+02:00")
    }

    @Test func gpsCoordinatesSurvive() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try StillExporter.export(
            fileAt: heicFixture, toDirectory: dir, filename: "out.heic")

        let props = try properties(of: out.url)
        let gps = try #require(props[kCGImagePropertyGPSDictionary] as? [CFString: Any])
        let lat = try #require(gps[kCGImagePropertyGPSLatitude] as? Double)
        let lon = try #require(gps[kCGImagePropertyGPSLongitude] as? Double)
        #expect(abs(lat - 47.7949) < 0.001)
        #expect(abs(lon - 13.0475) < 0.001)
        #expect(gps[kCGImagePropertyGPSLatitudeRef] as? String == "N")
        #expect(gps[kCGImagePropertyGPSLongitudeRef] as? String == "E")
    }

    @Test func makeAndModelSurvive() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try StillExporter.export(
            fileAt: heicFixture, toDirectory: dir, filename: "out.heic")

        let props = try properties(of: out.url)
        let tiff = try #require(props[kCGImagePropertyTIFFDictionary] as? [CFString: Any])
        // The same-device neighbour rule is how a GPS-less frame gets its timezone.
        #expect(tiff[kCGImagePropertyTIFFMake] as? String == "Apple")
        #expect(tiff[kCGImagePropertyTIFFModel] as? String == "iPhone 17 Pro")
    }

    @Test func orientationSurvives() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let sourceProps = try properties(of: heicFixture)
        let out = try StillExporter.export(
            fileAt: heicFixture, toDirectory: dir, filename: "out.heic")

        let props = try properties(of: out.url)
        #expect(
            props[kCGImagePropertyOrientation] as? Int
                == sourceProps[kCGImagePropertyOrientation] as? Int)
    }

    /// A downscaled file claiming the original's dimensions would be a measurement it cannot
    /// support — the same class of defect as emitting a confidence the pipeline never computed.
    @Test func reportedPixelDimensionsMatchTheActualImage() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try StillExporter.export(
            fileAt: heicFixture, toDirectory: dir, filename: "out.heic")

        let props = try properties(of: out.url)
        #expect(props[kCGImagePropertyPixelWidth] as? Int == out.pixelWidth)
        #expect(props[kCGImagePropertyPixelHeight] as? Int == out.pixelHeight)
        #expect(props[kCGImagePropertyPixelWidth] as? Int != 4032)
    }

    /// Assert identity, not presence: P06 found nine assets declared `kind: "video"` whose
    /// exported files were JPEGs under `.mov` names, past a schema check and 87 passing tests.
    @Test func exportedHEICIsActuallyAHEIC() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try StillExporter.export(
            fileAt: heicFixture, toDirectory: dir, filename: "out.heic")

        let source = try #require(CGImageSourceCreateWithURL(out.url as CFURL, nil))
        let type = try #require(CGImageSourceGetType(source) as String?)
        #expect(type == "public.heic", "exported container is \(type)")
    }

    /// A JPEG fixture proves the exporter is not accidentally HEIC-only, and that the container
    /// follows the source rather than a hardcoded default.
    @Test func jpegSourceStaysJpeg() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try StillExporter.export(
            fileAt: LibraryFixtures.url(forFixture: "jpeg_gps_no_offset.jpg"),
            toDirectory: dir, filename: "out.jpg")

        let source = try #require(CGImageSourceCreateWithURL(out.url as CFURL, nil))
        #expect(CGImageSourceGetType(source) as String? == "public.jpeg")
    }

    /// A fixture with no EXIF must export rather than throw — the pipeline degrades on missing
    /// metadata, so the exporter must not be the thing that aborts.
    @Test func imageWithoutExifStillExports() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let out = try StillExporter.export(
            fileAt: LibraryFixtures.url(forFixture: "jpeg_no_exif.jpg"),
            toDirectory: dir, filename: "out.jpg")
        #expect(FileManager.default.fileExists(atPath: out.url.path))
    }

    @Test func unreadableSourceThrows() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let notAnImage = dir.appending(path: "notes.txt")
        try "not media".write(to: notAnImage, atomically: true, encoding: .utf8)
        #expect(throws: (any Error).self) {
            try StillExporter.export(
                fileAt: notAnImage, toDirectory: dir, filename: "out.jpg")
        }
    }
}
