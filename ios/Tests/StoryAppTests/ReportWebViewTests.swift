import Foundation
import Testing
import WebKit

@testable import StoryApp

/// A real report bundle, produced by `story-book build` over the committed fixture media. See
/// `Fixtures/README.md` for the command that regenerates it.
enum Fixture {
    static var bundleRoot: URL {
        Bundle.module.resourceURL!.appending(path: "Fixtures/bundle")
    }

    static let firstDay = "2026-07-18"
    static let secondDay = "2026-07-19"

    /// A copy the test may mutate, deleted when the test ends.
    static func mutableCopy() throws -> URL {
        let copy = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.copyItem(at: bundleRoot, to: copy)
        return copy
    }
}

struct ReportBundleTests {
    @Test func acceptsARealReport() throws {
        let bundle = try ReportBundle(root: Fixture.bundleRoot)
        #expect(FileManager.default.fileExists(atPath: bundle.indexURL.path))
    }

    /// The read-access root is the directory *containing* `report/`. Handing `report/` to
    /// `loadFileURL` renders every page and breaks every image.
    @Test func rootIsTheParentOfTheReportDirectory() throws {
        let bundle = try ReportBundle(root: Fixture.bundleRoot)
        #expect(bundle.indexURL.deletingLastPathComponent() != bundle.root)
        #expect(bundle.indexURL.deletingLastPathComponent().lastPathComponent == "report")
    }

    @Test func rejectsADirectoryWithNoReport() throws {
        let empty = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: empty, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: empty) }
        #expect(throws: ReportBundle.Problem.missing("report/index.html")) {
            try ReportBundle(root: empty)
        }
    }

    /// Leaflet is vendored, not linked. A bundle that lost it still renders text and shows a blank
    /// square where the map was -- the failure mode a presence check on `index.html` misses.
    @Test func rejectsABundleMissingVendoredLeaflet() throws {
        let copy = try Fixture.mutableCopy()
        defer { try? FileManager.default.removeItem(at: copy) }
        try FileManager.default.removeItem(at: copy.appending(path: "report/vendor/leaflet.js"))
        #expect(throws: ReportBundle.Problem.missing("report/vendor/leaflet.js")) {
            try ReportBundle(root: copy)
        }
    }

    @Test func listsDayPagesInDateOrder() throws {
        let bundle = try ReportBundle(root: Fixture.bundleRoot)
        let names = try bundle.dayPages().map(\.lastPathComponent)
        #expect(names == ["\(Fixture.firstDay).html", "\(Fixture.secondDay).html"])
    }

    @Test func everyReferenceInARealReportResolves() throws {
        let bundle = try ReportBundle(root: Fixture.bundleRoot)
        #expect(try bundle.unresolvedReferences() == [])
    }

    /// The control for the test above: it has to be able to see a file go missing, or "no
    /// unresolved references" is a statement about the regex rather than about the bundle.
    @Test func aDeletedThumbnailIsReportedUnresolved() throws {
        let copy = try Fixture.mutableCopy()
        defer { try? FileManager.default.removeItem(at: copy) }
        let thumbs = copy.appending(path: "thumbs")
        let victim = try FileManager.default
            .contentsOfDirectory(at: thumbs, includingPropertiesForKeys: nil)
            .sorted { $0.lastPathComponent < $1.lastPathComponent }[0]
        try FileManager.default.removeItem(at: victim)

        let unresolved = try ReportBundle(root: copy).unresolvedReferences()
        #expect(unresolved.contains { $0.reference.hasSuffix(victim.lastPathComponent) })
        #expect(unresolved.allSatisfy { $0.reason == .missing })
    }

    /// Video posters live under `.cache/video/`, and a bundle assembled by copying `report/`,
    /// `thumbs/` and `previews/` -- the three obvious directories -- loses them silently.
    @Test func videoPostersAreReferencedFromOutsideTheObviousDirectories() throws {
        let bundle = try ReportBundle(root: Fixture.bundleRoot)
        let day = try String(contentsOf: bundle.dayPage(Fixture.firstDay), encoding: .utf8)
        #expect(day.contains("../../.cache/video/"))
        #expect(day.contains("_poster.jpg"))
    }

    /// Tested directly, not only through its callers. This project has been bitten three times by
    /// a measurement helper being the bug while the code around it read correctly.
    @Test func resolvesADayPageReferenceToTheThumbnailDirectory() {
        #expect(
            ReportBundle.resolve(reference: "../../thumbs/a.jpg", fromPageIn: ["report", "days"])
                == ["thumbs", "a.jpg"])
    }

    @Test func resolvesAnIndexReferenceToTheThumbnailDirectory() {
        #expect(
            ReportBundle.resolve(reference: "../thumbs/a.jpg", fromPageIn: ["report"])
                == ["thumbs", "a.jpg"])
    }

    @Test func resolvesASiblingDayPage() {
        #expect(
            ReportBundle.resolve(reference: "2026-07-19.html", fromPageIn: ["report", "days"])
                == ["report", "days", "2026-07-19.html"])
    }

    @Test func refusesAReferenceThatClimbsOutOfTheBundle() {
        #expect(ReportBundle.resolve(reference: "../../../etc/hosts", fromPageIn: ["report", "days"]) == nil)
    }

    @Test func aReferenceOutsideTheBundleIsNotTreatedAsPresent() throws {
        let copy = try Fixture.mutableCopy()
        defer { try? FileManager.default.removeItem(at: copy) }
        let index = copy.appending(path: "report/index.html")
        var html = try String(contentsOf: index, encoding: .utf8)
        html.append("<img src=\"../../../etc/hosts\">")
        try html.write(to: index, atomically: true, encoding: .utf8)

        let unresolved = try ReportBundle(root: copy).unresolvedReferences()
        #expect(unresolved.contains { $0.reason == .outsideBundle })
    }
}

/// Serialized on purpose. Every `WKWebView` in the process shares one WebContent process, its
/// resource cache and its file-access grants, so tests that load overlapping URLs in parallel
/// contaminate each other -- the read-access test below passed alone and failed alongside its
/// neighbours, which is the wrong way round for a test to be wrong.
@MainActor
@Suite(.serialized)
struct ReportRenderingTests {
    /// Blocks every network load inside the webview. This is what makes "offline" a property of
    /// the test rather than of whichever machine it runs on.
    ///
    /// Compiled once and cached: two tests compiling the same identifier at the same time fails
    /// with "Mapping file failed", which is a race in the harness masquerading as a product bug.
    static func offlineRules() async throws -> WKContentRuleList {
        if let compiled { return compiled }
        let json = """
            [{"trigger": {"url-filter": "^https?://"}, "action": {"type": "block"}}]
            """
        let list = try await WKContentRuleListStore.default()
            .compileContentRuleList(forIdentifier: "offline", encodedContentRuleList: json)!
        compiled = list
        return list
    }

    private static var compiled: WKContentRuleList?

    /// Each test gets its own copy of the bundle, so each loads distinct `file://` URLs.
    ///
    /// Not tidiness. WebKit's resource cache is shared across every `WKWebView` in the process, so
    /// the test that proves a *narrow* read-access root starves the images passed alone and failed
    /// in parallel: another test had already loaded the same URLs with full access and the images
    /// came from cache. A test that can be satisfied by another test's side effect is proving
    /// nothing about the code under it.
    static func loaded(day: String) async throws -> (ReportLoader, URL) {
        let copy = try Fixture.mutableCopy()
        let loader = try await self.loader(root: copy)
        loader.load(day: day)
        try await settle(loader.webView)
        return (loader, copy)
    }

    static func loader(
        root: URL,
        offline: Bool = true,
        openExternal: @escaping @MainActor (URL) -> Void = { _ in }
    ) async throws -> ReportLoader {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        if offline {
            configuration.userContentController.add(try await offlineRules())
        }
        let loader = ReportLoader(
            bundle: try ReportBundle(root: root),
            configuration: configuration,
            openExternal: openExternal)
        loader.webView.frame = .init(x: 0, y: 0, width: 1024, height: 1400)
        return loader
    }

    static func settle(_ webView: WKWebView) async throws {
        var waited = 0
        while webView.isLoading, waited < 400 {
            try await Task.sleep(nanoseconds: 25_000_000)
            waited += 1
        }
        #expect(!webView.isLoading, "page never finished loading")
        // Leaflet initialises from a script at the end of the document; give the run loop a turn.
        try await Task.sleep(nanoseconds: 300_000_000)
    }

    /// A click returns before the navigation it triggers has begun, so `isLoading` is still false
    /// when `settle` looks. Poll the URL instead of guessing at a sleep.
    static func waitForPage(_ name: String, in webView: WKWebView) async throws {
        var waited = 0
        while webView.url?.lastPathComponent != name, waited < 120 {
            try await Task.sleep(nanoseconds: 25_000_000)
            waited += 1
        }
        #expect(webView.url?.lastPathComponent == name)
    }

    static func number(_ js: String, in webView: WKWebView) async throws -> Int {
        let value = try await webView.evaluateJavaScript(js)
        return (value as? NSNumber)?.intValue ?? -1
    }

    /// **The criterion, and the one assertion that cannot be replaced by a cheaper one.** A report
    /// whose markup is perfect and whose images all 404 passes every structural check there is;
    /// `naturalWidth` is decoded pixels, so it can only be non-zero if the bytes arrived.
    @Test func everyImageOnADayPageActuallyDecodes() async throws {
        let (loader, copy) = try await Self.loaded(day: Fixture.firstDay)
        defer { try? FileManager.default.removeItem(at: copy) }
        let webView = loader.webView

        let total = try await Self.number("document.querySelectorAll('figure.shot img').length", in: webView)
        let decoded = try await Self.number(
            "Array.from(document.querySelectorAll('figure.shot img')).filter(i => i.naturalWidth > 0).length",
            in: webView)
        #expect(total > 0, "fixture day page has no photographs")
        #expect(decoded == total, "\(total - decoded) of \(total) images did not load")
    }

    /// The control for the test above. `naturalWidth > 0` is only evidence if it can also be zero:
    /// delete one thumbnail and exactly one image must stop decoding, with the rest unaffected.
    ///
    /// This began as the more direct control -- load the page granting read access to `report/`
    /// only, and watch every image starve. It fails: WebKit's file-access grants and its resource
    /// cache live in one WebContent process shared by every `WKWebView`, so once any earlier test
    /// has loaded the media the narrow grant is no longer observable. It passed run alone and
    /// failed run with its neighbours, and `.serialized` did not fix it because the leak is over
    /// the process lifetime, not the parallelism. A control that only holds when it runs first is
    /// not a control.
    @Test func aMissingThumbnailIsTheOnlyImageThatFailsToDecode() async throws {
        let copy = try Fixture.mutableCopy()
        defer { try? FileManager.default.removeItem(at: copy) }
        let bundle = try ReportBundle(root: copy)
        let page = try String(contentsOf: bundle.dayPage(Fixture.firstDay), encoding: .utf8)
        let referenced = try #require(
            try NSRegularExpression(pattern: "thumbs/([0-9a-f]+\\.jpg)")
                .firstMatch(in: page, range: NSRange(page.startIndex..., in: page))
                .flatMap { Range($0.range(at: 1), in: page).map { String(page[$0]) } })
        try FileManager.default.removeItem(at: copy.appending(path: "thumbs/\(referenced)"))

        let loader = try await Self.loader(root: copy)
        loader.load(day: Fixture.firstDay)
        let webView = loader.webView
        try await Self.settle(webView)

        let broken = try await Self.number(
            "Array.from(document.querySelectorAll('figure.shot img')).filter(i => i.naturalWidth === 0).length",
            in: webView)
        // Counted by source, not by element: a thumbnail that is both a day highlight and a
        // timeline entry appears twice, so one deleted file breaks two `<img>`s.
        let brokenElsewhere = try await Self.number(
            """
            Array.from(document.querySelectorAll('figure.shot img'))
                 .filter(i => i.naturalWidth === 0 && !i.src.endsWith('\(referenced)')).length
            """, in: webView)
        #expect(broken > 0, "the deleted thumbnail still decoded")
        #expect(brokenElsewhere == 0, "an image unrelated to the deleted file also failed to decode")
    }

    @Test func theVideoPosterDecodesFromItsCacheDirectory() async throws {
        let (loader, copy) = try await Self.loaded(day: Fixture.firstDay)
        defer { try? FileManager.default.removeItem(at: copy) }
        let webView = loader.webView

        let posters = try await Self.number(
            """
            Array.from(document.querySelectorAll('img'))
                 .filter(i => i.src.includes('_poster.jpg') && i.naturalWidth > 0).length
            """, in: webView)
        #expect(posters > 0, "no video poster decoded")
    }

    /// The map, with the network off: Leaflet is vendored so it loads from disk, and the route and
    /// the markers are drawn from data embedded in the page.
    @Test func theMapDrawsItsRouteWithNoNetwork() async throws {
        let (loader, copy) = try await Self.loaded(day: Fixture.firstDay)
        defer { try? FileManager.default.removeItem(at: copy) }
        let webView = loader.webView

        #expect(try await Self.number("typeof L === 'undefined' ? 0 : 1", in: webView) == 1)
        #expect(try await Self.number("document.querySelectorAll('#map path.leaflet-interactive').length", in: webView) > 0)
        #expect(try await Self.number("document.querySelectorAll('#map .leaflet-marker-pane path, #map path.leaflet-interactive').length", in: webView) > 0)
    }

    /// The control for the test above. Leaflet still creates the tile `<img>` elements offline;
    /// if any of them had decoded, the network was reachable and "offline" proved nothing.
    @Test func noMapTileDecodesWithTheNetworkBlocked() async throws {
        let (loader, copy) = try await Self.loaded(day: Fixture.firstDay)
        defer { try? FileManager.default.removeItem(at: copy) }
        let webView = loader.webView

        let tiles = try await Self.number("document.querySelectorAll('img.leaflet-tile').length", in: webView)
        let decoded = try await Self.number(
            "Array.from(document.querySelectorAll('img.leaflet-tile')).filter(i => i.naturalWidth > 0).length",
            in: webView)
        #expect(tiles > 0, "Leaflet never asked for a tile, so this proves nothing")
        #expect(decoded == 0, "a tile decoded -- the network was not actually blocked")
    }

    @Test func dayNavigationLoadsTheNextDay() async throws {
        let (loader, copy) = try await Self.loaded(day: Fixture.firstDay)
        defer { try? FileManager.default.removeItem(at: copy) }
        let webView = loader.webView

        _ = try await webView.evaluateJavaScript(
            "document.querySelector('a[href=\"\(Fixture.secondDay).html\"]').click()")
        try await Self.waitForPage("\(Fixture.secondDay).html", in: webView)
    }

    @Test func theCrumbReturnsToTheIndex() async throws {
        let (loader, copy) = try await Self.loaded(day: Fixture.firstDay)
        defer { try? FileManager.default.removeItem(at: copy) }
        let webView = loader.webView

        _ = try await webView.evaluateJavaScript("document.querySelector('p.crumb a').click()")
        try await Self.waitForPage("index.html", in: webView)
    }

    /// The OpenStreetMap link leaves the report. Following it in the webview strands the reader on
    /// a web page with no route back into a `file://` bundle.
    @Test func anExternalLinkIsHandedOutAndNotFollowed() async throws {
        let copy = try Fixture.mutableCopy()
        defer { try? FileManager.default.removeItem(at: copy) }
        let opened = Opened()
        let loader = try await Self.loader(root: copy, openExternal: { opened.record($0) })
        loader.load(day: Fixture.firstDay)
        let webView = loader.webView
        try await Self.settle(webView)
        let before = webView.url

        _ = try await webView.evaluateJavaScript("document.querySelector('a.osm').click()")
        try await Task.sleep(nanoseconds: 500_000_000)

        #expect(opened.urls.first?.host?.contains("openstreetmap") == true)
        #expect(webView.url == before, "the webview navigated away from the report")
    }

    /// The webview cannot outlive its delegate, because it is the loader's own property.
    ///
    /// This is a regression test for a bug in *this file*: a helper built a webview, attached a
    /// local `ReportLoader` and returned only the webview. `navigationDelegate` is weak, so the
    /// loader died at the return and every link in the report went dead -- images still decoded,
    /// the map still drew, and two navigation tests failed while five passed. The fix was to make
    /// the loader own the webview, so there is no longer an order of operations to get wrong.
    @Test func theWebViewIsOwnedByItsLoader() async throws {
        let copy = try Fixture.mutableCopy()
        defer { try? FileManager.default.removeItem(at: copy) }
        let loader = try await Self.loader(root: copy)
        loader.load(day: Fixture.firstDay)
        try await Self.settle(loader.webView)
        #expect(loader.webView.navigationDelegate === loader)
    }

    @MainActor
    final class Opened {
        private(set) var urls: [URL] = []
        func record(_ url: URL) { urls.append(url) }
    }
}
