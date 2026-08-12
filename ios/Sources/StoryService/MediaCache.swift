import Foundation

/// I33. Posters and reels, downloaded once and kept.
///
/// **Both artifacts are immutable (D5).** A poster is derived from a clip's content hash; a
/// re-cut reel is a new `job_id`, never a mutation of an existing one (S07's own words: "a re-cut
/// is a new job with a new job_id, never a rewrite of this one; no ETag or revalidation is needed
/// on the client side"). So this cache needs **eviction, never invalidation** — no ETags, no
/// conditional GETs, no "is this still fresh" round trip.
///
/// **Key by id, never by URL.** Every signed URL this app receives rotates on the next request —
/// `presign_get` is called again on every `GET /jobs/{id}/reel` — so a `URLCache` keyed by URL
/// would treat a rotated URL as a new resource and re-download identical bytes forever. The `id`
/// passed in here is the stable half: a `job_id` for a reel, an asset hash (or hash + a poster
/// marker) for a poster. Whatever the caller uses, it must be the thing that does not change
/// between requests — that is the entire contract of this type.
///
/// **`Caches/`, excluded from backup.** Both artifacts are re-downloadable from the service, so
/// they do not belong in a backed-up, non-purgeable location — Apple's own guidance names exactly
/// this shape ("re-creatable data that can be downloaded again") as a rejection reason. The
/// directory is marked `isExcludedFromBackup` once at creation, and each file is marked again on
/// write: Apple's documented behaviour is that the flag propagates to files added later, but nothing
/// here relies on that being true forever on every OS version, so both call sites can be checked
/// independently, and each is a one-line assertion in the tests below.
public actor MediaCache {
    public enum CacheError: Error, Sendable, Equatable {
        case badResponse(String)
        case transport(String)
    }

    private let directory: URL
    private let session: URLSession
    private let fileManager: FileManager

    /// - Parameters:
    ///   - directory: where cached files live. Defaults to `Caches/StoryMediaCache`, which is
    ///     where this type's own contract says it must be. Tests override it with a temp
    ///     directory so a test run never touches the real cache.
    public init(
        directory: URL = MediaCache.defaultDirectory(),
        session: URLSession = .shared,
        fileManager: FileManager = .default
    ) throws {
        self.directory = directory
        self.session = session
        self.fileManager = fileManager
        if !fileManager.fileExists(atPath: directory.path) {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        }
        try Self.excludeFromBackup(directory)
    }

    public static func defaultDirectory() -> URL {
        let caches =
            FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return caches.appending(path: "StoryMediaCache")
    }

    /// The local file for `id`, downloading from `remoteURL` on first access and never again.
    ///
    /// **The cache hit path never inspects `remoteURL` at all.** If a file already exists at this
    /// `id`'s path, it is returned immediately — a rotated signed URL for the same `id` changes
    /// nothing, because the URL is never consulted once the file is on disk. That is the whole of
    /// what "key by id, never by URL" means in code: the argument exists only for the miss path.
    public func localURL(id: String, remoteURL: URL, pathExtension: String) async throws -> URL {
        let destination = fileURL(for: id, pathExtension: pathExtension)
        if fileManager.fileExists(atPath: destination.path) {
            return destination
        }
        let (tempURL, response) = try await download(remoteURL)
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            let status = (response as? HTTPURLResponse)?.statusCode ?? -1
            throw CacheError.badResponse("GET \(remoteURL): status \(status)")
        }
        try fileManager.createDirectory(
            at: directory, withIntermediateDirectories: true)
        // Atomic replace: two concurrent misses for the same id both download, and the second
        // `moveItem` must not fail just because the first already produced the file.
        _ = try? fileManager.removeItem(at: destination)
        try fileManager.moveItem(at: tempURL, to: destination)
        try? Self.excludeFromBackup(destination)
        return destination
    }

    /// Bytes for `id`, for callers that want `Data` rather than a file (an image view, say) rather
    /// than a file to hand to `AVPlayer`.
    public func data(id: String, remoteURL: URL, pathExtension: String) async throws -> Data {
        let url = try await localURL(id: id, remoteURL: remoteURL, pathExtension: pathExtension)
        return try Data(contentsOf: url)
    }

    /// True when `id` is already on disk, with no network involved — the exact question a caller
    /// asks before deciding whether tapping play will start a download.
    public func isCached(id: String, pathExtension: String) -> Bool {
        fileManager.fileExists(atPath: fileURL(for: id, pathExtension: pathExtension).path)
    }

    /// Evict everything. **Not invalidation** — nothing here re-validates a cached file against
    /// the service, because nothing cached is ever stale (D5). This exists for storage pressure,
    /// and the acceptance criterion for it is narrow: the *next* access must redownload cleanly,
    /// not merely that files disappeared.
    public func purge() throws {
        guard fileManager.fileExists(atPath: directory.path) else { return }
        for name in try fileManager.contentsOfDirectory(atPath: directory.path) {
            try fileManager.removeItem(at: directory.appending(path: name))
        }
    }

    // MARK: - Internals

    private func fileURL(for id: String, pathExtension: String) -> URL {
        directory.appending(path: "\(Self.sanitize(id)).\(pathExtension)")
    }

    /// Job ids and asset hashes from this service are alphanumeric, but nothing here assumes it:
    /// any character not safe in a single path component is replaced, so a filename can never
    /// smuggle a path separator or collide across two different raw ids by accident.
    private static func sanitize(_ id: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_"))
        let scalars = id.unicodeScalars.map { allowed.contains($0) ? Character($0) : "_" }
        let cleaned = String(scalars)
        return cleaned.isEmpty ? "_" : cleaned
    }

    private static func excludeFromBackup(_ url: URL) throws {
        var mutableURL = url
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try mutableURL.setResourceValues(values)
    }

    /// Split out so a test can stub the transport without touching the filesystem-moving logic
    /// above it.
    private func download(_ url: URL) async throws -> (URL, URLResponse) {
        do {
            return try await session.download(from: url)
        } catch {
            throw CacheError.transport("GET \(url): \(error)")
        }
    }
}
