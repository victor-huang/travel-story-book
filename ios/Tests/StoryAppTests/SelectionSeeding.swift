#if os(iOS)

    import Foundation
    import Photos

    /// A minimal, `StoryAppTests`-scoped stand-in for `PhotoExportTests/LibraryFixtures.swift`.
    /// That file lives in a different SwiftPM test target and `Package.swift` (I01's contract)
    /// declares no dependency from this target onto it, so this duplicates just enough of it —
    /// two committed fixtures, one dedicated album — to seed real `PHAsset`s for I18's export
    /// test. Same reasons as `MusicStubHTTP` next to it: the real fake is `internal` to a target
    /// this one cannot see.
    ///
    /// **Only runs app-hosted**, identically to `LibraryFixtures`: PhotoKit authorization goes
    /// through TCC, which needs a bundle identity a bare SwiftPM test target does not have.
    /// `StoryBookHost.xcodeproj` currently wires only the `PhotoExportTests` scheme (D10) — this
    /// target has no scheme of its own yet, so `canSeed` gates every test below to a no-op until
    /// one exists. Recorded as a gap in the I18 log rather than silently skipped.
    enum SelectionSeeding {
        static let albumName = "storybook-test-fixtures-i18"
        static let fixtureA = "jpeg_no_gps.jpg"
        static let fixtureB = "jpeg_no_exif.jpg"

        static var canSeed: Bool {
            #if os(iOS)
                return true
            #else
                return false
            #endif
        }

        enum SeedingError: Error {
            case notAuthorized(PHAuthorizationStatus)
            case missingFromRepo(String)
            case notInLibrary(String)
        }

        private static var mediaDirectory: URL {
            URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()  // StoryAppTests
                .deletingLastPathComponent()  // Tests
                .deletingLastPathComponent()  // ios
                .deletingLastPathComponent()
                .appending(path: "tests/fixtures/media")
        }

        /// The two seeded assets, `(fixtureA, fixtureB)`, importing on first use. Idempotent: a
        /// second call finds the same album rather than re-importing.
        static func assets() async throws -> (PHAsset, PHAsset) {
            var status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
            if status == .notDetermined {
                status = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
            }
            guard status == .authorized || status == .limited else {
                throw SeedingError.notAuthorized(status)
            }

            if album() == nil {
                for filename in [fixtureA, fixtureB] {
                    let url = mediaDirectory.appending(path: filename)
                    guard FileManager.default.fileExists(atPath: url.path) else {
                        throw SeedingError.missingFromRepo(filename)
                    }
                }
                try await PHPhotoLibrary.shared().performChanges {
                    let albumRequest = PHAssetCollectionChangeRequest
                        .creationRequestForAssetCollection(withTitle: albumName)
                    let placeholders = [fixtureA, fixtureB].compactMap {
                        filename -> PHObjectPlaceholder? in
                        let request = PHAssetCreationRequest.forAsset()
                        let options = PHAssetResourceCreationOptions()
                        options.originalFilename = filename
                        request.addResource(
                            with: .photo, fileURL: mediaDirectory.appending(path: filename),
                            options: options)
                        return request.placeholderForCreatedAsset
                    }
                    albumRequest.addAssets(placeholders as NSArray)
                }
            }

            guard let assetA = lookUp(filename: fixtureA) else {
                throw SeedingError.notInLibrary(fixtureA)
            }
            guard let assetB = lookUp(filename: fixtureB) else {
                throw SeedingError.notInLibrary(fixtureB)
            }
            return (assetA, assetB)
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

#endif
