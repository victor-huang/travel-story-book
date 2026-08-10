import XCTest
import WebKit

final class ProbeTests: XCTestCase {
    @MainActor func testWebViewLoadsFileURL() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let f = dir.appendingPathComponent("i.html")
        try "<html><body><h1 id=t>hello</h1></body></html>".write(to: f, atomically: true, encoding: .utf8)
        let w = WKWebView(frame: .init(x: 0, y: 0, width: 400, height: 400))
        w.loadFileURL(f, allowingReadAccessTo: dir)
        var tries = 0
        while w.isLoading && tries < 200 { try await Task.sleep(nanoseconds: 50_000_000); tries += 1 }
        let r = try await w.evaluateJavaScript("document.getElementById('t').textContent") as? String
        XCTAssertEqual(r, "hello")
    }
}
