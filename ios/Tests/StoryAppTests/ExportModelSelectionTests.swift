#if os(iOS)

    import Foundation
    import Photos
    import Testing

    @testable import PhotoExport
    @testable import StoryApp

    /// I18's literal acceptance criterion: deselecting a thumbnail and exporting must produce a
    /// folder missing exactly that asset, proven against the folder `FolderWriter` actually wrote
    /// -- not merely that `includedAssets` looks right in memory -- and reselecting it must
    /// restore it to the next export.
    ///
    /// Gated on `SelectionSeeding.canSeed`, which is `false` off a bare `swift test` for the same
    /// TCC reason `LibraryFixturesTests` gates on `LibraryFixtures.canSeedLibrary`: a SwiftPM test
    /// target has no bundle identity to authorize against. Unlike `PhotoExportTests`, this target
    /// (`StoryAppTests`) has no Xcode scheme wired in `StoryBookHost.xcodeproj` yet (D10 covers
    /// only `PhotoExportTests`), so this suite is written and compiles but has not been *run*
    /// against a real seeded library in this session -- recorded in the I18 log rather than
    /// claimed.
    @MainActor
    @Suite(.serialized)
    struct ExportModelSelectionTests {
        private func tempDestinationParent() throws -> URL {
            let dir = FileManager.default.temporaryDirectory
                .appending(path: "export-model-selection-\(UUID().uuidString)")
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            return dir
        }

        @Test(.enabled(if: SelectionSeeding.canSeed))
        func deselectingAnAssetOmitsItFromTheExportedFolderAndReselectingRestoresIt() async throws {
            let (assetA, assetB) = try await SelectionSeeding.assets()
            let resourcesA = PHAssetResource.assetResources(for: assetA)
            let resourcesB = PHAssetResource.assetResources(for: assetB)
            let filenameA = try #require(resourcesA.first?.originalFilename)
            let filenameB = try #require(resourcesB.first?.originalFilename)

            let model = ExportModel()
            model.assets = [assetA, assetB]
            model.excludedAssetIDs = [assetA.localIdentifier]

            #expect(model.includedAssets.map(\.localIdentifier) == [assetB.localIdentifier])

            await model.export()
            let firstFolder = try #require(model.folder)
            defer { try? FileManager.default.removeItem(at: firstFolder) }

            #expect(
                !FileManager.default.fileExists(atPath: firstFolder.appending(path: filenameA).path),
                "the deselected asset must not be in the exported folder")
            #expect(
                FileManager.default.fileExists(atPath: firstFolder.appending(path: filenameB).path),
                "the included asset must be in the exported folder")

            // Reselect and export again -- a fresh destination, since the first run's ledger
            // would otherwise skip filenameB as already exported and mask whether filenameA
            // really comes back.
            model.excludedAssetIDs = []
            #expect(model.includedAssets.count == 2)

            await model.export()
            let secondFolder = try #require(model.folder)
            defer { try? FileManager.default.removeItem(at: secondFolder) }

            #expect(
                FileManager.default.fileExists(atPath: secondFolder.appending(path: filenameA).path),
                "reselecting must restore the asset to the next export")
            #expect(
                FileManager.default.fileExists(atPath: secondFolder.appending(path: filenameB).path))
        }

        /// The control the project's own house rule asks for: a set that excludes *nothing* must
        /// still write every asset, so the omission proven above is caused by the exclusion, not
        /// by `export()` dropping items for some unrelated reason.
        @Test(.enabled(if: SelectionSeeding.canSeed))
        func anEmptyExclusionSetExportsEverything() async throws {
            let (assetA, assetB) = try await SelectionSeeding.assets()
            let model = ExportModel()
            model.assets = [assetA, assetB]
            model.excludedAssetIDs = []

            await model.export()
            let folder = try #require(model.folder)
            defer { try? FileManager.default.removeItem(at: folder) }

            #expect(model.summary?.written == 2)
        }
    }

#endif
