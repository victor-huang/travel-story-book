import Foundation
import StoryKit
import Testing
import WebKit

@testable import StoryApp

struct AssetRequestParsingTests {
    @Test func parsesAThumbnailRequest() {
        let request = AssetRequestParsing.parse(URL(string: "storyasset://thumbs/abc123.jpg")!)
        #expect(request == AssetRequest(kind: .thumbnail, hashPrefix: "abc123"))
    }

    @Test func parsesAPreviewRequest() {
        let request = AssetRequestParsing.parse(URL(string: "storyasset://previews/abc123.jpg")!)
        #expect(request == AssetRequest(kind: .preview, hashPrefix: "abc123"))
    }

    /// `media_rel` prefixes media only. The stylesheet and vendored Leaflet stay relative
    /// (`TestMediaPrefixIsAParameter` asserts this on the Python side), so a `storyasset://`
    /// reference to either would mean that contract broke -- this parser should not paper over it
    /// by guessing at a mapping.
    @Test func rejectsNonMediaHosts() {
        #expect(AssetRequestParsing.parse(URL(string: "storyasset://style.css")!) == nil)
        #expect(AssetRequestParsing.parse(URL(string: "storyasset://vendor/leaflet.js")!) == nil)
    }

    @Test func rejectsAnotherScheme() {
        #expect(AssetRequestParsing.parse(URL(string: "https://thumbs/abc123.jpg")!) == nil)
    }

    @Test func rejectsANonJpegSuffix() {
        #expect(AssetRequestParsing.parse(URL(string: "storyasset://thumbs/abc123.png")!) == nil)
    }

    @Test func rejectsANonHexName() {
        #expect(AssetRequestParsing.parse(URL(string: "storyasset://thumbs/not-hex!!.jpg")!) == nil)
    }

    @Test func rejectsAnEmptyName() {
        #expect(AssetRequestParsing.parse(URL(string: "storyasset://thumbs/.jpg")!) == nil)
    }
}

struct AssetSchemeHandlerPlaceholderTests {
    @Test func theDefaultPlaceholderDecodesAsAnImage() {
        let data = AssetSchemeHandler.defaultPlaceholder()
        let source = CGImageSourceCreateWithData(data as CFData, nil)
        #expect(source != nil)
        #expect(CGImageSourceGetCount(source!) == 1)
    }

    @Test func thePlaceholderHonoursTheRequestedEdge() {
        let data = AssetSchemeHandler.defaultPlaceholder(edge: 64)
        let source = try! #require(CGImageSourceCreateWithData(data as CFData, nil))
        let image = try! #require(CGImageSourceCreateImageAtIndex(source, 0, nil))
        #expect(image.width == 64)
        #expect(image.height == 64)
    }
}

/// End to end: a real `WKWebView`, a real registered `WKURLSchemeHandler`, and the criterion this
/// project always asks for -- decoded pixels, not markup. Mirrors `ReportRenderingTests` (I24):
/// `naturalWidth > 0` is the only evidence the response actually reached the page.
@MainActor
@Suite(.serialized)
struct AssetSchemeHandlerRenderingTests {
    static func webView(_ handler: AssetSchemeHandler) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.setURLSchemeHandler(handler, forURLScheme: AssetSchemeHandler.scheme)
        return WKWebView(frame: .init(x: 0, y: 0, width: 400, height: 400), configuration: configuration)
    }

    static func load(_ html: String, in webView: WKWebView) async throws {
        webView.loadHTMLString(html, baseURL: URL(string: "https://example.invalid/"))
        var waited = 0
        while webView.isLoading, waited < 200 {
            try await Task.sleep(nanoseconds: 25_000_000)
            waited += 1
        }
        try await Task.sleep(nanoseconds: 300_000_000)
    }

    static func naturalWidth(in webView: WKWebView) async throws -> Int {
        let value = try await webView.evaluateJavaScript(
            "document.getElementById('t') ? document.getElementById('t').naturalWidth : -1")
        return (value as? NSNumber)?.intValue ?? -1
    }

    static let page = "<html><body><img id=t src=\"storyasset://thumbs/abc123.jpg\"></body></html>"

    @Test func tierOneLocalDataDecodes() async throws {
        let handler = AssetSchemeHandler(local: { _ in AssetSchemeHandler.defaultPlaceholder(edge: 10) })
        let webView = Self.webView(handler)
        try await Self.load(Self.page, in: webView)
        #expect(try await Self.naturalWidth(in: webView) == 10)
    }

    /// The control for the test above and the one below: each tier must be able to say "no" and
    /// have the next one asked, or "every tier is required" is not actually being enforced.
    @Test func tierOneMissFallsThroughToTierTwo() async throws {
        let handler = AssetSchemeHandler(
            local: { _ in nil },
            remote: { _ in AssetSchemeHandler.defaultPlaceholder(edge: 20) })
        let webView = Self.webView(handler)
        try await Self.load(Self.page, in: webView)
        #expect(try await Self.naturalWidth(in: webView) == 20)
    }

    @Test func bothTiersMissFallsThroughToThePlaceholder() async throws {
        let handler = AssetSchemeHandler(local: { _ in nil }, remote: { _ in nil })
        let webView = Self.webView(handler)
        try await Self.load(Self.page, in: webView)
        let width = try await Self.naturalWidth(in: webView)
        #expect(width > 0, "the bundled placeholder did not decode")
    }

    /// A local hit must win over a remote one being available at all -- tier order, not "whichever
    /// answers first".
    @Test func aLocalHitIsPreferredOverARemoteOneEvenWhenBothWouldAnswer() async throws {
        let handler = AssetSchemeHandler(
            local: { _ in AssetSchemeHandler.defaultPlaceholder(edge: 11) },
            remote: { _ in AssetSchemeHandler.defaultPlaceholder(edge: 22) })
        let webView = Self.webView(handler)
        try await Self.load(Self.page, in: webView)
        #expect(try await Self.naturalWidth(in: webView) == 11)
    }

    /// The request itself reaches the resolver, not just "some" request -- so a handler that
    /// resolved every request identically would still fail this.
    @Test func theResolverSeesTheParsedRequestNotJustAnyRequest() async throws {
        let handler = AssetSchemeHandler(local: { request in
            request == AssetRequest(kind: .thumbnail, hashPrefix: "abc123")
                ? AssetSchemeHandler.defaultPlaceholder(edge: 33) : nil
        })
        let webView = Self.webView(handler)
        try await Self.load(Self.page, in: webView)
        #expect(try await Self.naturalWidth(in: webView) == 33)
    }

    /// Neither tier is even asked for a URL this scheme cannot parse -- `didFailWithError` rather
    /// than a resolver guessing at a request that does not exist.
    @Test func anUnparsableRequestNeverDecodes() async throws {
        var wasAsked = false
        let handler = AssetSchemeHandler(local: { _ in
            wasAsked = true
            return AssetSchemeHandler.defaultPlaceholder()
        })
        let webView = Self.webView(handler)
        let html = "<html><body><img id=t src=\"storyasset://videos/abc123.jpg\"></body></html>"
        try await Self.load(html, in: webView)
        #expect(try await Self.naturalWidth(in: webView) <= 0)
        #expect(!wasAsked, "the resolver was called for a request that should never have parsed")
    }

    @Test func aPreviewRequestReachesTheHandlerAsPreviewNotThumbnail() async throws {
        let handler = AssetSchemeHandler(local: { request in
            request.kind == .preview ? AssetSchemeHandler.defaultPlaceholder(edge: 44) : nil
        })
        let webView = Self.webView(handler)
        let html = "<html><body><img id=t src=\"storyasset://previews/abc123.jpg\"></body></html>"
        try await Self.load(html, in: webView)
        #expect(try await Self.naturalWidth(in: webView) == 44)
    }
}

#if canImport(Photos)
    import Photos

    /// `PhotoKitAssetSource` against a library with no matching, or no authorized, asset. This
    /// runs on the macOS test host, where PhotoKit is linkable but unauthenticated -- so it
    /// exercises the same code path a deleted or iCloud-only asset would take: `PHAsset.fetchAssets`
    /// comes back empty and tier 1 must say so rather than throw.
    ///
    /// The positive path -- real bytes, dimensions reflecting the requested target size, and a
    /// video asset being skipped in favour of tier 2 -- needs a seeded simulator library and
    /// currently has no host: `StoryBookHost.xcodeproj` wires only `PhotoExportTests` (D10), and
    /// `StoryAppTests` is not a scheme in it. Recorded as a gap rather than silently skipped.
    @MainActor
    struct PhotoKitAssetSourceTests {
        @Test func anEmptyLedgerReturnsNilWithoutTouchingPhotoKit() async {
            let source = PhotoKitAssetSource(ledger: ExportLedger())
            let result = await source.data(
                for: AssetRequest(kind: .thumbnail, hashPrefix: "0123456789abcdef"))
            #expect(result == nil)
        }

        /// An entry that names an asset the library does not have -- deleted, wrong device, or
        /// simply no authorization here -- must resolve to nil, not crash or hang.
        @Test func aLedgerEntryForAnAssetTheLibraryCannotProduceReturnsNil() async {
            var ledger = ExportLedger()
            ledger.record(
                .init(
                    localIdentifier: "not-a-real-asset-id",
                    contentHash: String(repeating: "ab", count: 64),
                    originalFilename: "IMG_0001.HEIC"))
            let source = PhotoKitAssetSource(ledger: ledger)
            let result = await source.data(
                for: AssetRequest(kind: .thumbnail, hashPrefix: String(repeating: "ab", count: 8)))
            #expect(result == nil)
        }
    }
#endif
