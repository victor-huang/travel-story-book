import CoreGraphics
import Foundation
import ImageIO
import StoryKit
import UniformTypeIdentifiers
import WebKit

#if canImport(Photos)
    import Photos
#endif
#if canImport(UIKit)
    import UIKit
#endif

/// What a `storyasset://` request is asking for: `storyasset://thumbs/<hash-prefix>.jpg` for the
/// grid, `storyasset://previews/<hash-prefix>.jpg` for the tap-through.
///
/// The service renders the report with `media_rel="storyasset://"` (XT-1) and the downloaded
/// bundle carries no `thumbs/` or `previews/` directory at all -- there is no reason to download a
/// 1080px copy of a photograph whose original already lives on the phone (D4). Every reference in
/// `_shot.html` becomes one of these.
public struct AssetRequest: Sendable, Equatable, CustomStringConvertible {
    public enum Kind: String, Sendable, Equatable {
        case thumbnail, preview
    }

    public let kind: Kind
    /// A prefix of the BLAKE2b content hash -- `asset_id`, or long enough to contain it.
    public let hashPrefix: String

    public init(kind: Kind, hashPrefix: String) {
        self.kind = kind
        self.hashPrefix = hashPrefix
    }

    public var description: String { "\(kind.rawValue)/\(hashPrefix)" }
}

public enum AssetRequestParsing {
    /// `nil` for anything that is not a `thumbs/` or `previews/` reference under this scheme --
    /// which includes the stylesheet and vendored Leaflet. `media_rel` prefixes media only; a
    /// Python-side test (`TestMediaPrefixIsAParameter`) asserts `style.css` never carries it, so a
    /// request for it here would mean that contract broke, not that this parser should catch it.
    public static func parse(_ url: URL) -> AssetRequest? {
        guard url.scheme == AssetSchemeHandler.scheme else { return nil }
        let kind: AssetRequest.Kind
        switch url.host {
        case "thumbs": kind = .thumbnail
        case "previews": kind = .preview
        default: return nil
        }
        let name = url.lastPathComponent
        guard name.hasSuffix(".jpg") else { return nil }
        let hashPrefix = String(name.dropLast(4))
        guard !hashPrefix.isEmpty, hashPrefix.allSatisfy(\.isHexDigit) else { return nil }
        return AssetRequest(kind: kind, hashPrefix: hashPrefix)
    }
}

/// Resolves every `storyasset://` request the report webview makes, in the order I25 specifies,
/// and every tier is required:
///
/// 1. **Local `PHAsset`** via the export ledger. Piping original bytes into a webview grid is how
///    the app gets jetsam-killed, so this asks `PHImageManager` for a *target size* rather than
///    the source data: `480` for the grid, `1600` for the tap-through -- the same long edges
///    `ThumbnailStage` renders server-side (`config.py`'s `thumbnail_long_edge` /
///    `preview_long_edge`), so a reader does not see a quality jump between tiers.
/// 2. **The server preview**, cached locally (I33). Not wired yet -- I33 does not exist -- so the
///    default resolver is `{ _ in nil }` and every miss falls straight to tier 3 until it is.
/// 3. **A bundled placeholder.** The fallback is not optional: the original may be in iCloud
///    rather than on device, deleted, or on another phone entirely. Synthesized with ImageIO
///    rather than shipped as an asset-catalog image, so this file needs no Xcode project and is
///    testable the same way `StillExporter` is.
@MainActor
public final class AssetSchemeHandler: NSObject, WKURLSchemeHandler {
    public nonisolated static let scheme = "storyasset"

    /// Mirrors `ReportConfig.thumbnail_long_edge` / `preview_long_edge` (`config.py:139-140`).
    public static let thumbnailTargetSize = CGSize(width: 480, height: 480)
    public static let previewTargetSize = CGSize(width: 1600, height: 1600)

    public enum AssetSchemeError: Error, Equatable {
        case unrecognizedRequest
    }

    private let local: @MainActor (AssetRequest) async -> Data?
    private let remote: @MainActor (AssetRequest) async -> Data?
    private let placeholder: Data

    /// In-flight work, keyed by task identity, so `stop` can cancel it and so a result that
    /// arrives after `stop` was called is dropped rather than delivered to a task WebKit has
    /// already discarded -- calling a `WKURLSchemeTask` method after that traps.
    private var activeTasks: [ObjectIdentifier: Task<Void, Never>] = [:]

    public init(
        local: @escaping @MainActor (AssetRequest) async -> Data? = { _ in nil },
        remote: @escaping @MainActor (AssetRequest) async -> Data? = { _ in nil },
        placeholder: Data? = nil
    ) {
        self.local = local
        self.remote = remote
        self.placeholder = placeholder ?? Self.defaultPlaceholder()
    }

    public func webView(_ webView: WKWebView, start urlSchemeTask: WKURLSchemeTask) {
        guard let url = urlSchemeTask.request.url, let request = AssetRequestParsing.parse(url)
        else {
            urlSchemeTask.didFailWithError(AssetSchemeError.unrecognizedRequest)
            return
        }
        let key = ObjectIdentifier(urlSchemeTask)
        activeTasks[key] = Task { [local, remote, placeholder] in
            var data = await local(request)
            if data == nil { data = await remote(request) }
            let resolved = data ?? placeholder
            guard !Task.isCancelled, self.activeTasks[key] != nil else { return }
            self.activeTasks[key] = nil
            self.deliver(resolved, to: urlSchemeTask, originalURL: url)
        }
    }

    public func webView(_ webView: WKWebView, stop urlSchemeTask: WKURLSchemeTask) {
        let key = ObjectIdentifier(urlSchemeTask)
        activeTasks[key]?.cancel()
        activeTasks[key] = nil
    }

    private func deliver(_ data: Data, to task: WKURLSchemeTask, originalURL: URL) {
        let response = URLResponse(
            url: originalURL, mimeType: "image/jpeg", expectedContentLength: data.count,
            textEncodingName: nil)
        task.didReceive(response)
        task.didReceive(data)
        task.didFinish()
    }

    // MARK: - Placeholder

    /// A flat gray square, encoded with ImageIO rather than `UIImage`/`NSImage` so this runs
    /// identically in CI on macOS and on device. Not meant to look like anything in particular --
    /// only to decode, so the report shows a blank tile rather than the browser's broken-image
    /// glyph on an asset neither the phone nor the network could produce.
    nonisolated static func defaultPlaceholder(edge: Int = 480, gray: CGFloat = 0.85) -> Data {
        let colorSpace = CGColorSpaceCreateDeviceGray()
        guard
            let context = CGContext(
                data: nil, width: edge, height: edge, bitsPerComponent: 8, bytesPerRow: 0,
                space: colorSpace, bitmapInfo: CGImageAlphaInfo.none.rawValue)
        else {
            return Data()
        }
        context.setFillColor(gray: gray, alpha: 1)
        context.fill(CGRect(x: 0, y: 0, width: edge, height: edge))
        guard let image = context.makeImage() else { return Data() }

        let data = NSMutableData()
        guard
            let destination = CGImageDestinationCreateWithData(
                data, UTType.jpeg.identifier as CFString, 1, nil)
        else {
            return Data()
        }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { return Data() }
        return data as Data
    }
}

#if canImport(Photos)

    /// Tier 1: the export ledger plus `PHImageManager`.
    ///
    /// **`isNetworkAccessAllowed` is deliberately `false`**, the opposite of `ResourceSelection`'s
    /// export path. Export is a deliberate, one-time background job where correctness outweighs
    /// latency, so it waits on iCloud. This resolves a live, scrolling grid: stalling one tile on
    /// an iCloud fetch is worse than falling through to tier 2, which exists for exactly the case
    /// I25 names -- "the original may be in iCloud rather than on device".
    @MainActor
    public final class PhotoKitAssetSource {
        private let ledger: ExportLedger

        public init(ledger: ExportLedger) {
            self.ledger = ledger
        }

        public func resolver() -> @MainActor (AssetRequest) async -> Data? {
            { [self] request in await self.data(for: request) }
        }

        public func data(for request: AssetRequest) async -> Data? {
            guard let entry = ledger.entries(matchingAssetId: request.hashPrefix).first else {
                return nil
            }
            let fetched = PHAsset.fetchAssets(
                withLocalIdentifiers: [entry.localIdentifier], options: nil)
            guard let asset = fetched.firstObject else { return nil }
            // Video posters have no phone equivalent: `_derived_images` returns the server's own
            // extracted frame as both thumbnail and preview for a video
            // (`pipeline/timeline.py:366-368`), and PhotoKit choosing a different frame every
            // build would make the grid inconsistent with what the pipeline decided. Stills local,
            // posters and reel from the CDN -- by design, not by omission.
            guard asset.mediaType != .video else { return nil }
            return await Self.imageData(for: asset, kind: request.kind)
        }

        #if canImport(UIKit)
            private static func imageData(for asset: PHAsset, kind: AssetRequest.Kind) async -> Data? {
                await withCheckedContinuation { continuation in
                    let options = PHImageRequestOptions()
                    // A single, final delivery rather than opportunistic degraded-then-final: the
                    // continuation can only resume once.
                    options.deliveryMode = .highQualityFormat
                    options.resizeMode = .fast
                    options.isNetworkAccessAllowed = false
                    options.isSynchronous = false
                    let targetSize =
                        kind == .thumbnail
                        ? AssetSchemeHandler.thumbnailTargetSize : AssetSchemeHandler.previewTargetSize
                    PHImageManager.default().requestImage(
                        for: asset, targetSize: targetSize, contentMode: .aspectFit,
                        options: options
                    ) { image, _ in
                        continuation.resume(returning: image?.jpegData(compressionQuality: 0.85))
                    }
                }
            }
        #else
            private static func imageData(for asset: PHAsset, kind: AssetRequest.Kind) async -> Data? {
                nil
            }
        #endif
    }

#endif
