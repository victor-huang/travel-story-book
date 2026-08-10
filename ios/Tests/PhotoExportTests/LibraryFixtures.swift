import Foundation
import Photos

/// I05: seeds a `PHPhotoLibrary` from the committed fixtures, so export tests exercise real
/// `PHAsset`s rather than a simulator library that has no HEIC-with-GPS-and-offset and proves
/// nothing.
///
/// Seeding uses `PHAssetCreationRequest` with the fixture file as the original resource — the
/// same shape of library entry the app will read — so the bytes and metadata a test sees are
/// the committed fixture's own. Import is idempotent: fixtures land in one album, found by
/// name on the next run.
///
/// **This only runs app-hosted.** PhotoKit authorization goes through TCC, and TCC attributes a
/// request to a bundle identity; a bare SwiftPM test target has none, so authorization fails
/// with "unable to construct an identity to kTCCServicePhotos" — nothing to grant *to*, which
/// no `simctl privacy grant` can fix. Hence `StoryBookHost.xcodeproj` (tracker D10). Run with:
///
///     xcodebuild test -project StoryBookHost.xcodeproj -scheme PhotoExportTests \
///       -destination 'platform=iOS Simulator,name=iPhone 17 Pro'
public enum LibraryFixtures {
    public static let albumName = "storybook-test-fixtures"

    /// Synthetic fixtures, rebuilt by `tests/fixtures/generate.py`.
    public static let stills = [
        "heic_gps_offset.heic",
        "jpeg_gps_no_offset.jpg",
        "jpeg_no_gps.jpg",
        "jpeg_no_exif.jpg",
        "burst_a.jpg",
        "burst_b.jpg",
    ]
    public static let clips = [
        "clip_speech.mov",
        "clip_silent.mp4",
        "clip_apple_export.mov",
    ]

    /// Real iPhone captures. Not regenerable — see `tests/fixtures/device_media/README.md`.
    /// These are the only fixtures carrying a real HEIC container and a spatial-audio track.
    public static let deviceStills = ["iphone_heic_gps_offset.heic"]
    public static let deviceClips = ["iphone_spatial_audio.mov"]

    public static var allFixtures: [String] {
        stills + clips + deviceStills + deviceClips
    }

    private static var repoRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // PhotoExportTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // ios
            .deletingLastPathComponent()
    }

    public static var mediaDirectory: URL {
        repoRoot.appending(path: "tests/fixtures/media")
    }

    public static var deviceMediaDirectory: URL {
        repoRoot.appending(path: "tests/fixtures/device_media")
    }

    /// Fixtures are committed artifacts: resolve by name, and let a missing one be an error
    /// rather than a skip.
    public static func url(forFixture filename: String) -> URL {
        let device = deviceStills + deviceClips
        return device.contains(filename)
            ? deviceMediaDirectory.appending(path: filename)
            : mediaDirectory.appending(path: filename)
    }

    private static func isVideo(_ filename: String) -> Bool {
        (clips + deviceClips).contains(filename)
    }

    /// The seeded asset for a fixture filename, importing the whole set on first use.
    /// Callers assert on the returned asset's properties, never on the import having
    /// "succeeded": a creation request that silently dropped metadata still returns an asset.
    public static func asset(forFixture filename: String) async throws -> PHAsset {
        try await seedIfNeeded()
        guard let asset = lookUp(filename: filename) else {
            throw FixtureError.notInLibrary(filename)
        }
        return asset
    }

    public enum FixtureError: Error {
        case missingFromRepo(String)
        case notAuthorized(PHAuthorizationStatus)
        case notInLibrary(String)
        case refusingToSeedHostLibrary
    }

    /// Seeding is confined to iOS — a simulator or a device. On the mac test host there *is* a
    /// real `PHPhotoLibrary`: the developer's own. An earlier version of this file had no such
    /// guard, and one `swift test` imported all eleven fixtures and an album straight into
    /// `~/Pictures/Photos Library.photoslibrary`. The tests passed, which is what made it
    /// dangerous. Gate the tests on this rather than discovering it in someone's Photos app.
    public static var canSeedLibrary: Bool {
        #if os(iOS)
            return true
        #else
            return false
        #endif
    }

    private static func seedIfNeeded() async throws {
        // Belt and braces: the tests are gated on `canSeedLibrary`, and seeding refuses anyway.
        // A guard that only lives in the caller is one refactor away from being gone.
        guard canSeedLibrary else { throw FixtureError.refusingToSeedHostLibrary }
        // Ask only when the answer is genuinely unknown. Calling requestAuthorization
        // unconditionally prompts even when access is already granted -- if the app holds
        // add-only, iOS raises an "additional access" upgrade dialog, which in a headless test
        // run blocks forever and takes the simulator down with it. `simctl privacy grant photos`
        // makes this branch a no-op; nothing here can dismiss a dialog once it is up.
        var status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        if status == .notDetermined {
            status = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
        }
        guard status == .authorized || status == .limited else {
            throw FixtureError.notAuthorized(status)
        }
        if album() != nil { return }

        for filename in allFixtures {
            let url = url(forFixture: filename)
            guard FileManager.default.fileExists(atPath: url.path) else {
                throw FixtureError.missingFromRepo(filename)
            }
        }

        try await PHPhotoLibrary.shared().performChanges {
            let albumRequest = PHAssetCollectionChangeRequest
                .creationRequestForAssetCollection(withTitle: albumName)
            let placeholders = allFixtures.compactMap { filename -> PHObjectPlaceholder? in
                let request = PHAssetCreationRequest.forAsset()
                let options = PHAssetResourceCreationOptions()
                // Preserved because overrides.toml addresses media by filename.
                options.originalFilename = filename
                request.addResource(
                    with: isVideo(filename) ? .video : .photo,
                    fileURL: url(forFixture: filename),
                    options: options)
                return request.placeholderForCreatedAsset
            }
            albumRequest.addAssets(placeholders as NSArray)
        }
    }

    private static func album() -> PHAssetCollection? {
        let options = PHFetchOptions()
        options.predicate = NSPredicate(format: "title = %@", albumName)
        return PHAssetCollection.fetchAssetCollections(
            with: .album, subtype: .albumRegular, options: options
        ).firstObject
    }

    private static func lookUp(filename: String) -> PHAsset? {
        guard let album = album() else { return nil }
        let assets = PHAsset.fetchAssets(in: album, options: nil)
        for index in 0..<assets.count {
            let asset = assets.object(at: index)
            let resources = PHAssetResource.assetResources(for: asset)
            if resources.contains(where: { $0.originalFilename == filename }) {
                return asset
            }
        }
        return nil
    }
}
