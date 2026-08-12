import Foundation
import Testing

@testable import StoryService

/// I33. The acceptance criterion is two measurements, and each gets its own control:
///
/// 1. *A rotated signed URL produces a cache hit* — proven against a control that a **different
///    id** at the very same URL does not hit cache, so the hit above is demonstrably about the id
///    and not about something coincidental in the URL or the test's bookkeeping.
/// 2. *A purged cache redownloads without error* — proven against a control that the redownload
///    actually happened (the call count goes up), not merely that no error was thrown.
struct MediaCacheTests {
    private func makeCache(host: String) throws -> MediaCache {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "MediaCacheTests-\(UUID().uuidString)")
        return try MediaCache(directory: directory, session: StubHTTP.session())
    }

    @Test func rotatedSignedURLIsACacheHitKeyedByID() async throws {
        let host = StubHTTP.uniqueHost()
        StubHTTP.register(host: host) { _ in
            .init(status: 200, body: Data("video bytes".utf8), headers: [:])
        }
        let cache = try makeCache(host: host)

        let firstURL = URL(string: "https://\(host)/reel.mp4?sig=aaa&expires=1")!
        let first = try await cache.localURL(id: "job-123", remoteURL: firstURL, pathExtension: "mp4")
        #expect(StubHTTP.recorded(host: host).count == 1)

        // Same id, deliberately a different URL string (a rotated signature) -- must hit cache,
        // never touch the network again.
        let rotatedURL = URL(string: "https://\(host)/reel.mp4?sig=zzz&expires=999")!
        let second = try await cache.localURL(
            id: "job-123", remoteURL: rotatedURL, pathExtension: "mp4")

        #expect(second == first)
        #expect(StubHTTP.recorded(host: host).count == 1, "a rotated URL must not re-fetch")
        #expect(try Data(contentsOf: second) == Data("video bytes".utf8))
    }

    @Test func differentIDAtTheSameURLIsAMiss() async throws {
        // The control for the test above: without it, a cache that (bug) keyed by URL instead of
        // id would also pass "rotated URL is a hit" trivially, because it never distinguishes ids
        // at all. A different id at the *same* URL must still cause a fresh fetch.
        let host = StubHTTP.uniqueHost()
        StubHTTP.register(host: host) { _ in .init(status: 200, body: Data("bytes".utf8)) }
        let cache = try makeCache(host: host)
        let url = URL(string: "https://\(host)/reel.mp4?sig=same")!

        _ = try await cache.localURL(id: "job-A", remoteURL: url, pathExtension: "mp4")
        #expect(StubHTTP.recorded(host: host).count == 1)

        _ = try await cache.localURL(id: "job-B", remoteURL: url, pathExtension: "mp4")
        #expect(StubHTTP.recorded(host: host).count == 2, "a different id must not reuse job-A's file")
    }

    @Test func purgedCacheRedownloadsWithoutError() async throws {
        let host = StubHTTP.uniqueHost()
        StubHTTP.register(host: host) { _ in .init(status: 200, body: Data("poster".utf8)) }
        let cache = try makeCache(host: host)
        let url = URL(string: "https://\(host)/poster.jpg")!

        _ = try await cache.localURL(id: "poster-1", remoteURL: url, pathExtension: "jpg")
        #expect(StubHTTP.recorded(host: host).count == 1)
        #expect(await cache.isCached(id: "poster-1", pathExtension: "jpg"))

        try await cache.purge()
        #expect(await cache.isCached(id: "poster-1", pathExtension: "jpg") == false)

        // The control: this must be a real second fetch, not a no-op that happens to leave no error.
        let redownloaded = try await cache.localURL(id: "poster-1", remoteURL: url, pathExtension: "jpg")
        #expect(StubHTTP.recorded(host: host).count == 2, "purge must force a real redownload")
        #expect(try Data(contentsOf: redownloaded) == Data("poster".utf8))
    }

    @Test func dataConvenienceReturnsTheCachedBytes() async throws {
        let host = StubHTTP.uniqueHost()
        StubHTTP.register(host: host) { _ in .init(status: 200, body: Data("jpeg-ish".utf8)) }
        let cache = try makeCache(host: host)
        let url = URL(string: "https://\(host)/poster.jpg")!

        let data = try await cache.data(id: "poster-2", remoteURL: url, pathExtension: "jpg")
        #expect(data == Data("jpeg-ish".utf8))
        #expect(StubHTTP.recorded(host: host).count == 1)

        // A second read must not touch the network.
        _ = try await cache.data(id: "poster-2", remoteURL: url, pathExtension: "jpg")
        #expect(StubHTTP.recorded(host: host).count == 1)
    }

    @Test func aFailedFetchThrowsAndLeavesNothingCached() async throws {
        let host = StubHTTP.uniqueHost()
        StubHTTP.register(host: host) { _ in .init(status: 404, body: Data()) }
        let cache = try makeCache(host: host)
        let url = URL(string: "https://\(host)/missing.mp4")!

        await #expect(throws: MediaCache.CacheError.self) {
            _ = try await cache.localURL(id: "job-missing", remoteURL: url, pathExtension: "mp4")
        }
        #expect(await cache.isCached(id: "job-missing", pathExtension: "mp4") == false)
    }

    @Test func theCacheDirectoryIsExcludedFromBackup() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "MediaCacheTests-\(UUID().uuidString)")
        _ = try MediaCache(directory: directory, session: .shared)

        let values = try directory.resourceValues(forKeys: [.isExcludedFromBackupKey])
        #expect(values.isExcludedFromBackup == true)
    }

    @Test func aDownloadedFileIsAlsoExcludedFromBackupIndividually() async throws {
        // Belt and braces, per the doc comment: this is checked independently of the directory
        // flag rather than assumed to be inherited.
        let host = StubHTTP.uniqueHost()
        StubHTTP.register(host: host) { _ in .init(status: 200, body: Data("x".utf8)) }
        let cache = try makeCache(host: host)
        let url = URL(string: "https://\(host)/reel.mp4")!

        let file = try await cache.localURL(id: "job-backup", remoteURL: url, pathExtension: "mp4")
        let values = try file.resourceValues(forKeys: [.isExcludedFromBackupKey])
        #expect(values.isExcludedFromBackup == true)
    }
}
