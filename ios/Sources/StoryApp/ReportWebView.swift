import Foundation
import WebKit

#if canImport(SwiftUI)
    import SwiftUI
#endif

/// The report as it exists on disk: `report/index.html`, a page per day, the stylesheet, vendored
/// Leaflet -- and the media the pages point *outside* that directory at.
///
/// The last part is the whole reason this type exists. Every image reference in a rendered report
/// climbs out of `report/`: `../thumbs/...` from the index, `../../thumbs/...`,  `../../previews/...`
/// and `../../.cache/video/..._poster.jpg` from a day page. So the directory that must be handed to
/// `loadFileURL(_:allowingReadAccessTo:)` is the one *containing* `report/`, never `report/` itself.
/// Get that wrong and the report renders perfectly with every image broken -- which is exactly the
/// failure this project has already shipped once, from a validator that checked the markup and
/// never resolved a reference.
public struct ReportBundle: Sendable, Equatable {
    /// A file the report points at that is not there, or not inside the bundle.
    public struct Unresolved: Sendable, Equatable, CustomStringConvertible {
        public let page: String
        public let reference: String
        public let reason: Reason

        public enum Reason: String, Sendable {
            case missing
            case outsideBundle
        }

        public var description: String { "\(page) -> \(reference) (\(reason.rawValue))" }
    }

    public enum Problem: Error, Equatable, CustomStringConvertible {
        case notADirectory(URL)
        case missing(String)

        public var description: String {
            switch self {
            case let .notADirectory(url): "not a directory: \(url.path)"
            case let .missing(name): "report bundle is missing \(name)"
            }
        }
    }

    /// The directory containing `report/`. This is the read-access root, not the index's parent.
    public let root: URL

    /// Files without which the report is silently wrong rather than visibly absent: it renders
    /// unstyled, or the map element stays blank because Leaflet never loaded.
    static let required = [
        "report/index.html",
        "report/style.css",
        "report/vendor/leaflet.js",
        "report/vendor/leaflet.css",
    ]

    public init(root: URL) throws {
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: root.path, isDirectory: &isDirectory),
            isDirectory.boolValue
        else {
            throw Problem.notADirectory(root)
        }
        self.root = root.standardizedFileURL
        for name in Self.required where !FileManager.default.fileExists(atPath: file(name).path) {
            throw Problem.missing(name)
        }
    }

    private func file(_ relative: String) -> URL {
        root.appending(path: relative)
    }

    /// Resolve a relative reference from a page's directory, as path components inside the bundle.
    /// Returns `nil` if it climbs out of the bundle.
    ///
    /// Deliberately arithmetic on components rather than on file URLs. `/var` is a symlink to
    /// `/private/var`, and `standardizedFileURL` and `resolvingSymlinksInPath` each rewrite across
    /// it for some paths and not others -- the same directory came back as `/var/...` and a file
    /// inside it as `/private/var/...`, so a deleted thumbnail was reported as *outside* the
    /// bundle it was merely missing from. The reason was wrong while the file was right, which is
    /// the kind of nearly-correct answer that survives review. Components have no such semantics:
    /// this function does not touch the filesystem.
    static func resolve(reference: String, fromPageIn directory: [String]) -> [String]? {
        var components = directory
        for part in reference.split(separator: "/", omittingEmptySubsequences: true) {
            switch part {
            case ".":
                continue
            case "..":
                if components.isEmpty { return nil }
                components.removeLast()
            default:
                components.append(String(part))
            }
        }
        return components
    }

    public var indexURL: URL { file("report/index.html") }

    public func dayPage(_ date: String) -> URL { file("report/days/\(date).html") }

    /// The day pages, in date order -- which is filename order, since the report names them
    /// `YYYY-MM-DD.html`.
    public func dayPages() throws -> [URL] {
        let days = file("report/days")
        guard FileManager.default.fileExists(atPath: days.path) else { return [] }
        return try FileManager.default
            .contentsOfDirectory(at: days, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "html" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    /// Both spellings of both sides, for the `/private/var` reason above: a false negative here
    /// cancels a navigation the reader asked for.
    public func contains(_ url: URL) -> Bool {
        let roots = Set([root.path, root.resolvingSymlinksInPath().path])
        let target = url.standardizedFileURL
        let paths = [target.path, target.resolvingSymlinksInPath().path]
        return roots.contains { prefix in paths.contains { $0.hasPrefix(prefix + "/") } }
    }

    /// Every local `src`/`href` in every page, resolved against the filesystem.
    ///
    /// A truncated download and a complete one both produce readable HTML; only resolving the
    /// references tells them apart. Empty means the bundle is self-sufficient offline.
    public func unresolvedReferences() throws -> [Unresolved] {
        let pattern = try NSRegularExpression(pattern: "(?:src|href)=\"([^\"]+)\"")
        var found: [Unresolved] = []
        let pages: [(URL, [String])] =
            [(indexURL, ["report"])] + (try dayPages().map { ($0, ["report", "days"]) })
        for (page, directory) in pages {
            let html = try String(contentsOf: page, encoding: .utf8)
            let range = NSRange(html.startIndex..., in: html)
            for match in pattern.matches(in: html, range: range) {
                guard let captured = Range(match.range(at: 1), in: html) else { continue }
                let reference = String(html[captured])
                guard isLocal(reference) else { continue }
                let name = page.lastPathComponent
                let path = String(reference.split(separator: "#")[0])
                guard let components = Self.resolve(reference: path, fromPageIn: directory) else {
                    found.append(.init(page: name, reference: reference, reason: .outsideBundle))
                    continue
                }
                let target = components.reduce(root) { $0.appending(path: $1) }
                if !FileManager.default.fileExists(atPath: target.path) {
                    found.append(.init(page: name, reference: reference, reason: .missing))
                }
            }
        }
        return found
    }

    private func isLocal(_ reference: String) -> Bool {
        if reference.hasPrefix("#") || reference.hasPrefix("//") { return false }
        guard let scheme = URL(string: reference)?.scheme else { return true }
        return scheme.isEmpty
    }
}

/// Drives a `WKWebView` over a `ReportBundle`. All of I24's logic lives here rather than in the
/// SwiftUI wrapper, so it can be tested against a real webview and a real report with no host app.
///
/// The loader **owns** its webview rather than attaching to one. `navigationDelegate` is a weak
/// reference, so a loader created, attached and then let go leaves a webview that renders the page
/// and quietly stops navigating: day links, the crumb and the tap-through all become dead. That
/// happened here, in a test helper that returned the webview and dropped the loader. Owning the
/// webview makes the lifetime impossible to get wrong instead of documenting it.
@MainActor
public final class ReportLoader: NSObject {
    public let bundle: ReportBundle
    public let webView: WKWebView
    private let openExternal: @MainActor (URL) -> Void

    /// - Parameter openExternal: where a link that leaves the report goes. The report carries one
    ///   ("open this area in OpenStreetMap"); following it *inside* the webview would strand the
    ///   reader on a web page with no way back to a `file://` bundle.
    /// - Parameter assetScheme: registers `AssetSchemeHandler` (I25) for `storyasset://` on
    ///   `configuration` before the webview is created. `nil` when a caller already registered
    ///   its own -- `WKWebViewConfiguration` traps if a scheme is registered twice.
    public init(
        bundle: ReportBundle,
        configuration: WKWebViewConfiguration = WKWebViewConfiguration(),
        assetScheme: AssetSchemeHandler? = .init(),
        openExternal: @escaping @MainActor (URL) -> Void
    ) {
        self.bundle = bundle
        self.openExternal = openExternal
        if let assetScheme {
            configuration.setURLSchemeHandler(assetScheme, forURLScheme: AssetSchemeHandler.scheme)
        }
        self.webView = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        webView.navigationDelegate = self
    }

    public func loadIndex() {
        load(bundle.indexURL)
    }

    public func load(day date: String) {
        load(bundle.dayPage(date))
    }

    private func load(_ page: URL) {
        // The second argument is the bundle root, not the page's directory: every image reference
        // in the report resolves above `report/`.
        webView.loadFileURL(page, allowingReadAccessTo: bundle.root)
    }
}

extension ReportLoader: WKNavigationDelegate {
    public func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.isFileURL {
            // Day navigation, the crumb back to the index, and the tap-through to a full-size
            // preview. WebKit would refuse anything outside the read-access root anyway; refusing
            // it here means a bundle that points outside itself fails visibly, not as a blank page.
            decisionHandler(bundle.contains(url) ? .allow : .cancel)
            return
        }
        decisionHandler(.cancel)
        openExternal(url)
    }
}

#if canImport(SwiftUI)

    /// The book (D3): the existing HTML report, rendered by the renderer that already exists.
    /// Deliberately thin -- no logic hides in a view.
    @MainActor
    public struct ReportWebView {
        let bundle: ReportBundle
        let day: String?
        let openExternal: @MainActor (URL) -> Void

        public init(
            bundle: ReportBundle,
            day: String? = nil,
            openExternal: @escaping @MainActor (URL) -> Void
        ) {
            self.bundle = bundle
            self.day = day
            self.openExternal = openExternal
        }

        public func makeCoordinator() -> ReportLoader {
            ReportLoader(bundle: bundle, openExternal: openExternal)
        }

        @MainActor
        func makeWebView(coordinator: ReportLoader) -> WKWebView {
            if let day {
                coordinator.load(day: day)
            } else {
                coordinator.loadIndex()
            }
            return coordinator.webView
        }
    }

    #if canImport(UIKit)
        extension ReportWebView: UIViewRepresentable {
            public func makeUIView(context: Context) -> WKWebView {
                makeWebView(coordinator: context.coordinator)
            }

            public func updateUIView(_ webView: WKWebView, context: Context) {}
        }
    #elseif canImport(AppKit)
        extension ReportWebView: NSViewRepresentable {
            public func makeNSView(context: Context) -> WKWebView {
                makeWebView(coordinator: context.coordinator)
            }

            public func updateNSView(_ webView: WKWebView, context: Context) {}
        }
    #endif

#endif
