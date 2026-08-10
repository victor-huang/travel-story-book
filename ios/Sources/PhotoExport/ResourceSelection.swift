import Foundation
import Photos

/// Answers "which `PHAssetResource` do I actually export?" for both exporters.
///
/// Live Photos, slow-mo, cinematic mode, RAW+JPEG pairs and edited-vs-original each have an
/// answer, and **getting one wrong produces a file that looks fine and carries the wrong
/// timestamps** — a Live Photo's paired video is a three-second clip whose times are not the
/// photograph's. They are enumerated here rather than discovered later.
///
/// The decision is a pure function over `ResourceDescriptor`, so every case is a CI test with no
/// photo library involved (D11 makes a library unavailable there). `resolve(for:)` is the thin
/// PhotoKit adapter over it.
public enum ResourceSelection {
    /// One `PHAssetResource`, reduced to what the decision depends on.
    public struct ResourceDescriptor: Sendable, Equatable {
        public let type: PHAssetResourceType
        public let originalFilename: String

        public init(type: PHAssetResourceType, originalFilename: String) {
            self.type = type
            self.originalFilename = originalFilename
        }
    }

    public enum MediaKind: Sendable, Equatable {
        case image
        case video
    }

    public enum SelectionError: Error, Equatable {
        case noExportableResource(available: [String])
    }

    /// Bytes come from the *rendered* resource when the traveller edited the item — that is the
    /// picture they think they took. The **filename comes from the original resource**, because
    /// an edit's own filename is `FullSizeRender.heic`, and `overrides.toml` addresses media by
    /// filename: shipping the render's name would silently break every correction written on
    /// the laptop.
    public static func choose(
        from descriptors: [ResourceDescriptor], mediaKind: MediaKind
    ) throws -> (chosen: ResourceDescriptor, resolved: ResolvedResource) {
        let byType = { (type: PHAssetResourceType) in
            descriptors.first { $0.type == type }
        }

        let chosen: ResourceDescriptor
        let reason: String

        switch mediaKind {
        case .image:
            if let edited = byType(.fullSizePhoto) {
                // An edited still. `.photo` is still present and holds the untouched original.
                chosen = edited
                reason = "edited render (fullSizePhoto) preferred over the unedited original"
            } else if let original = byType(.photo) {
                // Covers the plain case and RAW+JPEG, where `.photo` is the JPEG/HEIC and
                // `.alternatePhoto` is the RAW. The pipeline wants the processed image: RAW is
                // an order of magnitude larger, needs development to look like anything, and
                // carries the same capture metadata.
                chosen = original
                reason = descriptors.contains { $0.type == .alternatePhoto }
                    ? "processed photo preferred over the RAW alternate"
                    : "the still"
            } else {
                throw SelectionError.noExportableResource(
                    available: descriptors.map { $0.originalFilename })
            }

        case .video:
            if let rendered = byType(.fullSizeVideo) {
                // Slow-motion and cinematic mode: `.video` is the untouched high-frame-rate
                // capture, `.fullSizeVideo` is the render with the speed ramp or focus applied.
                // The render is what the traveller sees in Photos.
                chosen = rendered
                reason = "rendered video (fullSizeVideo) preferred over the raw capture"
            } else if let original = byType(.video) {
                chosen = original
                reason = "the clip"
            } else {
                throw SelectionError.noExportableResource(
                    available: descriptors.map { $0.originalFilename })
            }
        }

        // A Live Photo's motion half is never the export. It is only ever reached by asking for
        // `.pairedVideo` explicitly, which nothing above does -- stated so the omission reads as
        // a decision rather than a gap.
        let namingResource =
            byType(mediaKind == .image ? .photo : .video) ?? chosen

        return (
            chosen,
            ResolvedResource(
                originalFilename: namingResource.originalFilename,
                kind: mediaKind == .image ? .still : .video,
                reason: reason)
        )
    }
}

extension ResourceSelection {
    /// The PhotoKit adapter. Reads the asset's resources and applies `choose`.
    public static func resolve(for asset: PHAsset) throws -> (
        resource: PHAssetResource, resolved: ResolvedResource
    ) {
        let resources = PHAssetResource.assetResources(for: asset)
        let descriptors = resources.map {
            ResourceDescriptor(type: $0.type, originalFilename: $0.originalFilename)
        }
        let kind: MediaKind = asset.mediaType == .video ? .video : .image
        let (chosen, resolved) = try choose(from: descriptors, mediaKind: kind)

        // Match back by identity of (type, filename); `choose` returns a descriptor, and the
        // caller needs the resource itself to read bytes from.
        guard
            let resource = resources.first(where: {
                $0.type == chosen.type && $0.originalFilename == chosen.originalFilename
            })
        else {
            throw SelectionError.noExportableResource(
                available: descriptors.map { $0.originalFilename })
        }
        return (resource, resolved)
    }

    /// Options every read must use. **`isNetworkAccessAllowed` is not optional**: with
    /// "Optimize iPhone Storage" the original lives in iCloud and the local copy is a
    /// placeholder, so a read without it fails on exactly the assets a real library is full of.
    public static func readOptions() -> PHAssetResourceRequestOptions {
        let options = PHAssetResourceRequestOptions()
        options.isNetworkAccessAllowed = true
        return options
    }
}
