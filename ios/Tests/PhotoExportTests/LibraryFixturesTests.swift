import AVFoundation
import Foundation
import Photos
import Testing

@testable import PhotoExport

/// I05's acceptance criterion: a test can obtain a `PHAsset` for the HEIC+GPS+offset fixture
/// and for the spatial-audio `.mov`. This is the harness proving itself; Wave 1 builds on it.
///
/// Serialized deliberately. swift-testing runs a suite in parallel by default, which would send
/// four concurrent callers into `seedIfNeeded()` — racing on one authorization request and one
/// album creation, and importing the fixture set up to four times.
@Suite(.serialized)
struct LibraryFixturesTests {
    /// Fixtures are committed artifacts: assert their presence, never skip on a proxy.
    @Test func everyDeclaredFixtureExistsInTheRepo() {
        for filename in LibraryFixtures.allFixtures {
            let url = LibraryFixtures.url(forFixture: filename)
            #expect(
                FileManager.default.fileExists(atPath: url.path),
                "declared fixture missing from the repo: \(filename)")
        }
    }

    @Test(.enabled(if: LibraryFixtures.canSeedLibrary)) func seedsAndFindsTheHEICWithGPSAndOffset() async throws {
        let asset = try await LibraryFixtures.asset(forFixture: "iphone_heic_gps_offset.heic")

        // Assert the properties the pipeline consumes, not merely that an asset came back: a
        // creation request that dropped metadata still returns an asset.
        #expect(asset.mediaType == .image)
        let creation = try #require(asset.creationDate, "PHAsset carries no creationDate")
        #expect(creation.timeIntervalSince1970 > 0)

        // The fixture's GPS was rewritten to Salzburg; if PhotoKit reports something else, the
        // library did not take the location from the file we handed it.
        let location = try #require(asset.location, "PHAsset carries no location")
        #expect(abs(location.coordinate.latitude - 47.7949) < 0.01)
        #expect(abs(location.coordinate.longitude - 13.0475) < 0.01)
    }

    @Test(.enabled(if: LibraryFixtures.canSeedLibrary)) func seedsAndFindsTheSpatialAudioClip() async throws {
        let asset = try await LibraryFixtures.asset(forFixture: "iphone_spatial_audio.mov")
        #expect(asset.mediaType == .video)
        // The clip is 11.54 s; a wrong duration means a different file came back.
        #expect(abs(asset.duration - 11.54) < 0.5)
    }

    /// The point of the spatial-audio fixture. Mapping every audio stream is what broke 58 of
    /// 69 real clips on the Python side, so the harness must be able to *see* the second track
    /// — a clip whose extra track PhotoKit silently dropped would test nothing.
    @Test(.enabled(if: LibraryFixtures.canSeedLibrary)) func spatialAudioClipStillCarriesBothAudioTracks() async throws {
        let asset = try await LibraryFixtures.asset(forFixture: "iphone_spatial_audio.mov")
        // AVAsset is not Sendable, so it cannot cross the continuation boundary on its own.
        // The box is confined to this test, which awaits the value before touching it.
        struct Unchecked<T>: @unchecked Sendable { let value: T }
        let boxed: Unchecked<AVAsset> = try await withCheckedThrowingContinuation { continuation in
            PHImageManager.default().requestAVAsset(forVideo: asset, options: nil) { av, _, _ in
                if let av {
                    continuation.resume(returning: Unchecked(value: av))
                } else {
                    continuation.resume(throwing: LibraryFixtures.FixtureError.notInLibrary(
                        "iphone_spatial_audio.mov"))
                }
            }
        }
        let audio = try await boxed.value.loadTracks(withMediaType: .audio)
        // Pair the measurement with a control that must differ: one track would mean the
        // spatial track was lost somewhere between the file and PhotoKit.
        #expect(audio.count >= 2, "expected stereo + spatial tracks, saw \(audio.count)")
    }

    /// Filename preservation is load-bearing: `overrides.toml` addresses media by filename, so
    /// `IMG_1815.mov` staying `IMG_1815.mov` is what lets laptop corrections carry across.
    @Test(.enabled(if: LibraryFixtures.canSeedLibrary)) func preservesOriginalFilename() async throws {
        let asset = try await LibraryFixtures.asset(forFixture: "iphone_heic_gps_offset.heic")
        let resources = PHAssetResource.assetResources(for: asset)
        #expect(resources.contains { $0.originalFilename == "iphone_heic_gps_offset.heic" })
    }
}
