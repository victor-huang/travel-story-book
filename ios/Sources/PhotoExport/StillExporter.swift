import CoreGraphics
import Foundation
import ImageIO
import Photos
import UniformTypeIdentifiers

/// Downscales a still to 1080px **carrying its capture metadata**, which is the whole job.
///
/// `PHImageManager`'s resized-image requests hand back a `UIImage` with **no EXIF at all**, and
/// most cross-platform resize pipelines strip it by default too. The pipeline cannot recover
/// from that: without `DateTimeOriginal` there are no days and no events, without
/// `OffsetTimeOriginal` the first branch of timezone resolution is gone, and without GPS there
/// is no geocode, no home filter and no map.
///
/// So the path is: original bytes → `CGImageSource` → thumbnail at 1080 → new file with the
/// source's own property dictionaries copied in. Bytes are read through a file rather than held
/// in memory, because this is where an app gets jetsam-killed.
public enum StillExporter {
    /// Fields the pipeline consumes, and who consumes them. Asserted as a set by the tests so
    /// that adding a consumer without a test is visible.
    public static let requiredMetadata = [
        "DateTimeOriginal",  // every day and event boundary
        "OffsetTimeOriginal",  // first branch of timezone resolution
        "GPSLatitude", "GPSLongitude",  // geocode, home filter, map, gps_backfill
        "Orientation",  // geometry.orientation, reel framing
        "Make", "Model",  // the same-device neighbour rule
    ]

    public struct Output: Sendable, Equatable {
        public let url: URL
        public let filename: String
        public let pixelWidth: Int
        public let pixelHeight: Int
    }

    public enum ExportError: Error {
        case unreadableSource
        case noImageInSource
        case downscaleFailed
        case destinationCreationFailed(String)
        case writeFailed
        case resourceReadFailed(String)
    }

    /// The default long edge. The service only ever sees this, which is also why the report
    /// resolves images back to the phone's originals (D4).
    public static let defaultMaxPixel = 1080

    // MARK: - The core, which needs no photo library

    /// Downscale `sourceURL` into `directory`, keeping `filename`.
    ///
    /// Orientation is **preserved as a tag rather than baked into the pixels**
    /// (`kCGImageSourceCreateThumbnailWithTransform: false`): the source folder is meant to look
    /// like what a camera wrote, and the pipeline reads `Orientation` to decide
    /// `geometry.orientation`. Rotating pixels and dropping the tag would give a correct-looking
    /// image and a wrong `geometry`.
    @discardableResult
    public static func export(
        fileAt sourceURL: URL,
        toDirectory directory: URL,
        filename: String,
        maxPixel: Int = defaultMaxPixel
    ) throws -> Output {
        guard let source = CGImageSourceCreateWithURL(sourceURL as CFURL, nil) else {
            throw ExportError.unreadableSource
        }
        guard CGImageSourceGetCount(source) > 0 else { throw ExportError.noImageInSource }

        let properties =
            CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any] ?? [:]

        let thumbnailOptions: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixel,
            kCGImageSourceCreateThumbnailWithTransform: false,
        ]
        guard
            let image = CGImageSourceCreateThumbnailAtIndex(
                source, 0, thumbnailOptions as CFDictionary)
        else {
            throw ExportError.downscaleFailed
        }

        // Keep the container the original used, so IMG_1815.HEIC stays a HEIC. Falling back to
        // JPEG would change what the pipeline's metadata code is handed.
        let type =
            CGImageSourceGetType(source).map { $0 as String }
            ?? UTType.jpeg.identifier
        let destinationURL = directory.appending(path: filename)
        guard
            let destination = CGImageDestinationCreateWithURL(
                destinationURL as CFURL, type as CFString, 1, nil)
        else {
            throw ExportError.destinationCreationFailed(type)
        }

        // The source's own dictionaries, minus the two that would now be lies: the original's
        // pixel dimensions. An artifact never overstates its contents, and a downscaled file
        // claiming 4032x3024 is exactly that.
        var carried = properties
        carried.removeValue(forKey: kCGImagePropertyPixelWidth)
        carried.removeValue(forKey: kCGImagePropertyPixelHeight)

        CGImageDestinationAddImage(destination, image, carried as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { throw ExportError.writeFailed }

        return Output(
            url: destinationURL,
            filename: filename,
            pixelWidth: image.width,
            pixelHeight: image.height)
    }

    // MARK: - Sources

    /// Export from either source (D12). The `PHAsset` case honours `ResourceSelection`, so an
    /// edited still exports its render under the original's filename.
    @discardableResult
    public static func export(
        _ source: ExportSource,
        toDirectory directory: URL,
        maxPixel: Int = defaultMaxPixel
    ) async throws -> Output {
        switch source {
        case .file(let url):
            return try export(
                fileAt: url, toDirectory: directory, filename: url.lastPathComponent,
                maxPixel: maxPixel)
        case .asset(let asset):
            let (resource, resolved) = try ResourceSelection.resolve(for: asset)
            let staged = try await ResourceSelection.stageToTemporaryFile(resource)
            defer { try? FileManager.default.removeItem(at: staged) }
            return try export(
                fileAt: staged, toDirectory: directory, filename: resolved.originalFilename,
                maxPixel: maxPixel)
        }
    }
}
