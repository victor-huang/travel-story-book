import Foundation
import Photos
import Testing

@testable import PhotoExport

/// I13's acceptance criterion: every variant has a test naming the resource chosen and why.
/// These run in CI — the decision is a pure function, and the `Photos` framework is available
/// on macOS even though a photo library is not.
struct ResourceSelectionTests {
    private typealias D = ResourceSelection.ResourceDescriptor

    @Test func plainStillExportsThePhoto() throws {
        let (chosen, resolved) = try ResourceSelection.choose(
            from: [D(type: .photo, originalFilename: "IMG_1815.HEIC")], mediaKind: .image)
        #expect(chosen.type == .photo)
        #expect(resolved.originalFilename == "IMG_1815.HEIC")
        #expect(resolved.kind == .still)
    }

    @Test func plainClipExportsTheVideo() throws {
        let (chosen, resolved) = try ResourceSelection.choose(
            from: [D(type: .video, originalFilename: "IMG_1816.MOV")], mediaKind: .video)
        #expect(chosen.type == .video)
        #expect(resolved.kind == .video)
    }

    /// A Live Photo is a still plus a paired video. Exporting the motion half would yield a
    /// three-second clip whose timestamps are not the photograph's.
    @Test func livePhotoExportsTheStillNotThePairedVideo() throws {
        let (chosen, resolved) = try ResourceSelection.choose(
            from: [
                D(type: .photo, originalFilename: "IMG_2264.HEIC"),
                D(type: .pairedVideo, originalFilename: "IMG_2264.MOV"),
            ], mediaKind: .image)
        #expect(chosen.type == .photo)
        #expect(resolved.originalFilename == "IMG_2264.HEIC")
    }

    /// `.photo` is the processed JPEG/HEIC, `.alternatePhoto` the RAW. RAW is far larger, needs
    /// development to look like anything, and carries the same capture metadata.
    @Test func rawPlusJpegExportsTheProcessedImage() throws {
        let (chosen, resolved) = try ResourceSelection.choose(
            from: [
                D(type: .photo, originalFilename: "IMG_1900.JPG"),
                D(type: .alternatePhoto, originalFilename: "IMG_1900.DNG"),
            ], mediaKind: .image)
        #expect(chosen.originalFilename == "IMG_1900.JPG")
        #expect(resolved.reason.contains("RAW"))
    }

    @Test func editedStillExportsTheRender() throws {
        let (chosen, _) = try ResourceSelection.choose(
            from: [
                D(type: .photo, originalFilename: "IMG_1815.HEIC"),
                D(type: .fullSizePhoto, originalFilename: "FullSizeRender.heic"),
            ], mediaKind: .image)
        #expect(chosen.type == .fullSizePhoto)
    }

    /// The subtlety that makes edits dangerous: the render's own filename is
    /// `FullSizeRender.heic`. `overrides.toml` addresses media by filename, so shipping that
    /// name would break every correction written on the laptop.
    @Test func editedStillKeepsTheOriginalFilename() throws {
        let (chosen, resolved) = try ResourceSelection.choose(
            from: [
                D(type: .photo, originalFilename: "IMG_1815.HEIC"),
                D(type: .fullSizePhoto, originalFilename: "FullSizeRender.heic"),
            ], mediaKind: .image)
        // Bytes from the render, name from the original -- they differ, which is the point.
        #expect(chosen.originalFilename == "FullSizeRender.heic")
        #expect(resolved.originalFilename == "IMG_1815.HEIC")
    }

    /// Slow-motion: `.video` is the untouched high-frame-rate capture, `.fullSizeVideo` the
    /// render with the speed ramp applied.
    @Test func slowMotionExportsTheRenderedVideo() throws {
        let (chosen, resolved) = try ResourceSelection.choose(
            from: [
                D(type: .video, originalFilename: "IMG_1820.MOV"),
                D(type: .fullSizeVideo, originalFilename: "FullSizeRender.mov"),
            ], mediaKind: .video)
        #expect(chosen.type == .fullSizeVideo)
        #expect(resolved.originalFilename == "IMG_1820.MOV")
    }

    @Test func cinematicModeExportsTheRenderedVideo() throws {
        let (chosen, _) = try ResourceSelection.choose(
            from: [
                D(type: .video, originalFilename: "IMG_1830.MOV"),
                D(type: .fullSizeVideo, originalFilename: "FullSizeRender.mov"),
                D(type: .adjustmentData, originalFilename: "adjustmentData"),
            ], mediaKind: .video)
        #expect(chosen.type == .fullSizeVideo)
    }

    /// A control that must differ: adjustment data is not an exportable resource, and an asset
    /// carrying only that must fail loudly rather than export a sidecar blob as a photograph.
    @Test func adjustmentDataAloneIsNotExportable() {
        #expect(throws: ResourceSelection.SelectionError.self) {
            try ResourceSelection.choose(
                from: [D(type: .adjustmentData, originalFilename: "adjustmentData")],
                mediaKind: .image)
        }
    }

    /// "Optimize iPhone Storage" leaves a placeholder on device and the original in iCloud. A
    /// read without network access fails on exactly the assets a real library is full of.
    @Test func readOptionsAllowNetworkForNonResidentAssets() {
        #expect(ResourceSelection.readOptions().isNetworkAccessAllowed)
    }
}
