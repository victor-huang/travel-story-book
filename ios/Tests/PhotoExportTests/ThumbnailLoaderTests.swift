#if os(iOS)

    import CoreGraphics
    import Testing
    import UIKit

    @testable import PhotoExport

    /// I18's acceptance criterion for the cache: a grid of ~50 assets scrolled twice must not
    /// request more than 50 thumbnails from the image manager. `PHImageManager` cannot be
    /// subclassed or driven without a real, authorized library (see `LibraryFixturesTests` /
    /// D10-D11), so these exercise `ThumbnailLoader` through `ThumbnailImageProviding`, the seam
    /// it is written against -- purely with synthetic identifiers, no `PHAsset` required.
    @MainActor
    @Suite
    struct ThumbnailLoaderTests {
        /// Records every call it receives and hands back a distinct 1x1 image per identifier, so
        /// a test can tell "the same image came back" from "a new request was made".
        actor CountingProvider: ThumbnailImageProviding {
            private(set) var calls: [String] = []

            func requestImage(identifier: String, targetSize: CGSize) async -> UIImage? {
                calls.append(identifier)
                return Self.image(tag: identifier)
            }

            static func image(tag: String) -> UIImage {
                let renderer = UIGraphicsImageRenderer(size: CGSize(width: 1, height: 1))
                return renderer.image { _ in }
            }
        }

        @Test func aSecondRequestForTheSameIdentifierAndSizeIsServedFromCache() async {
            let provider = CountingProvider()
            let loader = ThumbnailLoader(provider: provider)

            _ = await loader.thumbnail(forIdentifier: "asset-1", targetSize: ThumbnailLoader.gridTargetSize)
            _ = await loader.thumbnail(forIdentifier: "asset-1", targetSize: ThumbnailLoader.gridTargetSize)

            let calls = await provider.calls
            #expect(calls == ["asset-1"], "a re-request for a cached identifier must not reach the provider")
        }

        /// The control: a *second, cold* loader asked for the same identifier must still reach
        /// the provider. Without this, "one call" above could just mean the provider was never
        /// going to be asked twice for any reason -- this proves the cache, not the mock, is
        /// what suppressed the second call.
        @Test func aFreshLoaderWithNoCacheStillReachesTheProviderForTheSameIdentifier() async {
            let provider = CountingProvider()
            let first = ThumbnailLoader(provider: provider)
            let second = ThumbnailLoader(provider: provider)

            _ = await first.thumbnail(forIdentifier: "asset-1", targetSize: ThumbnailLoader.gridTargetSize)
            _ = await second.thumbnail(forIdentifier: "asset-1", targetSize: ThumbnailLoader.gridTargetSize)

            let calls = await provider.calls
            #expect(calls.count == 2, "a second, cache-less loader must still ask the provider")
        }

        /// Two different sizes for the same identifier (the grid tier and the detail tier) are
        /// two different requests -- a size-blind cache would serve a blown-up grid thumbnail to
        /// the full-screen page, or a downscaled detail image to the grid.
        @Test func theSameIdentifierAtTwoSizesIsCachedSeparately() async {
            let provider = CountingProvider()
            let loader = ThumbnailLoader(provider: provider)

            _ = await loader.thumbnail(forIdentifier: "asset-1", targetSize: ThumbnailLoader.gridTargetSize)
            _ = await loader.thumbnail(forIdentifier: "asset-1", targetSize: ThumbnailLoader.detailTargetSize)
            _ = await loader.thumbnail(forIdentifier: "asset-1", targetSize: ThumbnailLoader.gridTargetSize)

            let calls = await provider.calls
            #expect(calls.count == 2, "two distinct sizes must each be requested once, not shared")
        }

        /// The literal I18 "done when" line: ~50 seeded assets, two full scrolls, no more than 50
        /// requests. "Two full scrolls" is modelled as requesting the same 50 identifiers twice
        /// in sequence, the way `LazyVGrid` re-runs a cell's `.task(id:)` on reappearance.
        @Test func fiftyAssetsScrolledTwiceRequestNoMoreThanFiftyThumbnails() async {
            let provider = CountingProvider()
            let loader = ThumbnailLoader(provider: provider)
            let identifiers = (0..<50).map { "asset-\($0)" }

            for identifier in identifiers {
                _ = await loader.thumbnail(forIdentifier: identifier, targetSize: ThumbnailLoader.gridTargetSize)
            }
            for identifier in identifiers {
                _ = await loader.thumbnail(forIdentifier: identifier, targetSize: ThumbnailLoader.gridTargetSize)
            }

            let calls = await provider.calls
            #expect(calls.count == 50, "the second scroll must be served entirely from cache")
        }

        /// A miss (the provider found nothing -- a deleted or iCloud-only asset) must not be
        /// cached as a permanent failure: the next request for it is a fair retry, not a second
        /// silent miss forever.
        @Test func aMissIsNotCachedAndIsRetriedOnTheNextRequest() async {
            actor NilThenHitProvider: ThumbnailImageProviding {
                private(set) var callCount = 0
                func requestImage(identifier: String, targetSize: CGSize) async -> UIImage? {
                    callCount += 1
                    return callCount == 1 ? nil : CountingProvider.image(tag: identifier)
                }
            }
            let provider = NilThenHitProvider()
            let loader = ThumbnailLoader(provider: provider)

            let first = await loader.thumbnail(forIdentifier: "asset-1", targetSize: ThumbnailLoader.gridTargetSize)
            let second = await loader.thumbnail(forIdentifier: "asset-1", targetSize: ThumbnailLoader.gridTargetSize)

            #expect(first == nil)
            #expect(second != nil)
            let count = await provider.callCount
            #expect(count == 2, "a miss must be retried, not cached as a permanent nil")
        }
    }

#endif
