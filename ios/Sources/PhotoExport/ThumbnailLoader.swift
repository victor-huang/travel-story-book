#if os(iOS)

    import Photos
    import UIKit

    /// I18. A cached, async wrapper around `PHImageManager` for the selection review grid.
    ///
    /// **Why the cache is load-bearing, not an optimization.** `LazyVGrid` reissues its cell
    /// content every time a cell scrolls back into view — a bare `.task` calling
    /// `PHImageManager` directly on every appearance would ask the image manager for the same
    /// thumbnail again on every scroll, which is its own way to overload it on a few hundred
    /// assets (I18's spec, and the same jetsam risk `AssetSchemeHandler` names for original-
    /// resolution bytes — this is the identical discipline applied to a native `Image` instead
    /// of a webview `<img>`). An `NSCache` (not a plain dictionary: it evicts under memory
    /// pressure, which a photo grid is exactly the kind of view to create) makes a re-scroll a
    /// cache hit instead of a second request.
    ///
    /// **Delivery mode is `.highQualityFormat`, not `.opportunistic`.** Opportunistic delivery
    /// can call its handler twice — once with a fast, low-quality placeholder and again with the
    /// final image — which is right for a UIKit cell that redraws in place and wrong here: this
    /// API hands back one image per call, so a second, better delivery would either be dropped
    /// or require a callback-based API this loader deliberately avoids.
    @MainActor
    public final class ThumbnailLoader {
        /// A grid cell thumbnail. Fixed pixel size rather than derived from `UIScreen`/trait
        /// scale — nothing else in this module reads screen scale (`AssetSchemeHandler` uses a
        /// fixed 480pt tier for the same reason: one size served to every device is simpler than
        /// a size that silently varies with what ran the request), and 300px is comfortably
        /// above what a multi-column grid cell renders on any device this app targets.
        public static let gridTargetSize = CGSize(width: 300, height: 300)

        /// The full-screen single-asset page. Bigger than the grid cell so a tap-through does
        /// not look like a blown-up thumbnail, but still bounded — "display size", never the
        /// original. Matches the order of magnitude of `AssetSchemeHandler.previewTargetSize`
        /// (1600pt) without importing that type into this target.
        public static let detailTargetSize = CGSize(width: 1200, height: 1200)

        private let provider: any ThumbnailImageProviding
        private let cache = NSCache<NSString, UIImage>()

        public init(provider: any ThumbnailImageProviding = PHImageManagerThumbnailProvider()) {
            self.provider = provider
        }

        /// The public, `PHAsset`-facing entry point the grid and the detail page call.
        public func thumbnail(for asset: PHAsset, targetSize: CGSize = ThumbnailLoader.gridTargetSize) async -> UIImage? {
            await thumbnail(forIdentifier: asset.localIdentifier, targetSize: targetSize)
        }

        /// Identifier-only entry point. Kept apart from the one above so a test can drive the
        /// cache with synthetic identifiers, never needing a real `PHAsset` — which cannot be
        /// constructed outside an authorized library (I05/D10's finding) — to prove the cache
        /// works.
        func thumbnail(forIdentifier identifier: String, targetSize: CGSize) async -> UIImage? {
            let key = Self.cacheKey(identifier: identifier, targetSize: targetSize)
            if let cached = cache.object(forKey: key) {
                return cached
            }
            guard let image = await provider.requestImage(identifier: identifier, targetSize: targetSize)
            else {
                return nil
            }
            cache.setObject(image, forKey: key)
            return image
        }

        /// Size is part of the key, not just the identifier: the grid and the detail page ask
        /// for the same asset at two different sizes, and caching by identifier alone would let
        /// whichever tier asked first silently serve its (wrong-size) image to the other one.
        static func cacheKey(identifier: String, targetSize: CGSize) -> NSString {
            "\(identifier)@\(Int(targetSize.width))x\(Int(targetSize.height))" as NSString
        }
    }

    /// The seam `ThumbnailLoader` is tested through. `PHImageManager` cannot be subclassed or
    /// constructed off a real, authorized library (see `LibraryFixturesTests` and D10/D11), so a
    /// test that wants to prove caching behaviour — not PhotoKit's own behaviour — needs a fake
    /// that satisfies this protocol instead of a real image manager.
    public protocol ThumbnailImageProviding: Sendable {
        func requestImage(identifier: String, targetSize: CGSize) async -> UIImage?
    }

    /// The real implementation. Resolves the identifier to a `PHAsset` itself so the protocol
    /// above stays identifier-only — `PhotoKitAssetSource` (`AssetSchemeHandler.swift`) takes
    /// the same shape for the same reason.
    public struct PHImageManagerThumbnailProvider: ThumbnailImageProviding {
        public init() {}

        public func requestImage(identifier: String, targetSize: CGSize) async -> UIImage? {
            let fetched = PHAsset.fetchAssets(withLocalIdentifiers: [identifier], options: nil)
            guard let asset = fetched.firstObject else { return nil }
            return await withCheckedContinuation { continuation in
                let options = PHImageRequestOptions()
                // One final delivery, not opportunistic-then-final — see the type's doc comment.
                options.deliveryMode = .highQualityFormat
                options.resizeMode = .fast
                options.isSynchronous = false
                // A live, scrolling grid should not stall a tile on an iCloud fetch; the cell
                // shows its placeholder state instead. Same call `PhotoKitAssetSource` makes.
                options.isNetworkAccessAllowed = false
                PHImageManager.default().requestImage(
                    for: asset, targetSize: targetSize, contentMode: .aspectFill, options: options
                ) { image, _ in
                    continuation.resume(returning: image)
                }
            }
        }
    }

#endif
