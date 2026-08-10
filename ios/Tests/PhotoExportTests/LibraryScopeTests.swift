import Foundation
import Photos
import Testing

@testable import PhotoExport

/// The cull check is pure arithmetic over two counts, so it runs in CI. The scopes need a real
/// library and are gated on `LibraryFixtures.canSeedLibrary` (D9/D11) — on the mac host that
/// library would be the developer's own.
struct CullCheckTests {
    /// I10's acceptance criterion, in the human's own numbers: 60 of 800 nudges.
    @Test func aThinnedSelectionTriggersTheNudge() {
        let reading = CullCheck.reading(
            selected: CullCheck.anchorNudge.selected, inRange: CullCheck.anchorNudge.inRange)
        #expect(reading.verdict == .nudge)
    }

    /// The other half of the same criterion, and its control: a lightly-pruned selection must
    /// *not* nudge, or the check is a warning that always fires and will be ignored.
    @Test func aLightlyPrunedSelectionDoesNot() {
        let reading = CullCheck.reading(
            selected: CullCheck.anchorFine.selected, inRange: CullCheck.anchorFine.inRange)
        #expect(reading.verdict == .fine)
    }

    /// The threshold is unsettled (open question 5), so what is asserted is the **band**, not the
    /// placeholder: every threshold the two anchors admit separates them. If someone later
    /// replaces `provisionalNudgeBelow` with a measured value inside the band, this keeps
    /// passing; if they pick one outside it, this fails and says so.
    @Test func everyThresholdTheAnchorsAdmitSeparatesThem() {
        let lower = Double(CullCheck.anchorNudge.selected) / Double(CullCheck.anchorNudge.inRange)
        let upper = Double(CullCheck.anchorFine.selected) / Double(CullCheck.anchorFine.inRange)
        #expect(lower < upper, "the anchors do not bracket anything")

        for step in 1..<100 {
            let threshold = lower + (upper - lower) * Double(step) / 100
            let thin = CullCheck.reading(
                selected: CullCheck.anchorNudge.selected,
                inRange: CullCheck.anchorNudge.inRange, nudgeBelow: threshold)
            let light = CullCheck.reading(
                selected: CullCheck.anchorFine.selected,
                inRange: CullCheck.anchorFine.inRange, nudgeBelow: threshold)
            #expect(thin.verdict == .nudge, "threshold \(threshold) failed to nudge 60/800")
            #expect(light.verdict == .fine, "threshold \(threshold) nudged 600/800")
        }
    }

    @Test func theProvisionalThresholdIsInsideTheBandItClaims() {
        let lower = Double(CullCheck.anchorNudge.selected) / Double(CullCheck.anchorNudge.inRange)
        let upper = Double(CullCheck.anchorFine.selected) / Double(CullCheck.anchorFine.inRange)
        #expect(CullCheck.provisionalNudgeBelow > lower)
        #expect(CullCheck.provisionalNudgeBelow <= upper)
    }

    /// A reading that could not be taken must not read like one that came back clean. Returning
    /// `.fine` here would tell a user with a `.limited` grant that their selection is healthy on
    /// the strength of no evidence at all.
    @Test func anEmptySelectionIsNoEvidenceRatherThanFine() {
        #expect(CullCheck.reading(selected: 0, inRange: 0).verdict == .noEvidence)
    }

    @Test func aSelectionWithNothingToCompareAgainstIsNoEvidence() {
        #expect(CullCheck.reading(selected: 12, inRange: 0).verdict == .noEvidence)
    }

    /// The sentinel that must not compare cleanly against itself: `keptFraction` is `nil`, not
    /// `0.0`, when there was nothing to divide by.
    @Test func keptFractionIsAbsentRatherThanZeroWhenThereIsNothingToDivideBy() {
        #expect(CullCheck.reading(selected: 12, inRange: 0).keptFraction == nil)
        #expect(CullCheck.reading(selected: 0, inRange: 800).keptFraction == 0.0)
    }

    @Test func keptFractionReportsTheRawRatio() {
        let reading = CullCheck.reading(selected: 64, inRange: 274)
        #expect(reading.keptFraction == 64.0 / 274.0)
    }

    /// Everything a caller needs to say something true is on the reading. The UI must never have
    /// to invent a percentage.
    @Test func theReadingCarriesBothCounts() {
        let reading = CullCheck.reading(selected: 64, inRange: 274)
        #expect(reading.selected == 64)
        #expect(reading.inRange == 274)
    }
}

/// Fetch options are the part of scope selection that can be wrong without a library: a missing
/// sort descriptor or a predicate that lets audio through is a defect a seeded simulator would
/// only find later.
struct LibraryScopeFetchOptionsTests {
    @Test func ordersByCaptureTimeOldestFirst() throws {
        let sort = try #require(LibraryScope.fetchOptions().sortDescriptors?.first)
        #expect(sort.key == "creationDate")
        #expect(sort.ascending)
    }

    @Test func asksForImagesAndVideosOnly() throws {
        let predicate = try #require(LibraryScope.fetchOptions().predicate)
        let format = predicate.predicateFormat
        #expect(format.contains("\(PHAssetMediaType.image.rawValue)"))
        #expect(format.contains("\(PHAssetMediaType.video.rawValue)"))
        #expect(!format.contains("\(PHAssetMediaType.audio.rawValue)"))
    }

    @Test func anEmptyIdentifierSelectionFetchesNothing() {
        // Guarded because `fetchAssets(withLocalIdentifiers: [])` is a library call, and an empty
        // set has an answer that needs no library to know.
        #expect(LibraryScope.assets(in: .identifiers([])).isEmpty)
    }
}

/// Against the seeded library. Skipped where seeding would write to a real photo library.
struct LibraryScopeLibraryTests {
    @Test(.enabled(if: LibraryFixtures.canSeedLibrary))
    func aDateRangeCoveringTheFixturesFindsThem() async throws {
        let known = try await LibraryFixtures.asset(forFixture: "heic_gps_offset.heic")
        let taken = try #require(known.creationDate)
        let found = LibraryScope.assets(
            in: .dateRange(from: taken.addingTimeInterval(-1), to: taken.addingTimeInterval(1)))
        #expect(found.contains { $0.localIdentifier == known.localIdentifier })
    }

    @Test(.enabled(if: LibraryFixtures.canSeedLibrary))
    func aDateRangeBeforeEveryFixtureFindsNone() async throws {
        // The control: the range predicate has to be able to exclude, or "found them" says
        // nothing about the predicate.
        _ = try await LibraryFixtures.asset(forFixture: "heic_gps_offset.heic")
        let ancient = Date(timeIntervalSince1970: 0)
        let found = LibraryScope.assets(
            in: .dateRange(from: ancient, to: ancient.addingTimeInterval(60)))
        #expect(found.isEmpty)
    }

    @Test(.enabled(if: LibraryFixtures.canSeedLibrary))
    func identifiersFetchExactlyWhatWasAskedFor() async throws {
        let first = try await LibraryFixtures.asset(forFixture: "heic_gps_offset.heic")
        let second = try await LibraryFixtures.asset(forFixture: "jpeg_no_gps.jpg")
        let found = LibraryScope.assets(
            in: .identifiers([first.localIdentifier, second.localIdentifier]))
        #expect(Set(found.map(\.localIdentifier)) == [first.localIdentifier, second.localIdentifier])
    }

    @Test(.enabled(if: LibraryFixtures.canSeedLibrary))
    func identifiersComeBackInCaptureOrder() async throws {
        let first = try await LibraryFixtures.asset(forFixture: "heic_gps_offset.heic")
        let second = try await LibraryFixtures.asset(forFixture: "jpeg_no_gps.jpg")
        let found = LibraryScope.assets(
            in: .identifiers([second.localIdentifier, first.localIdentifier]))
        let dates = found.compactMap(\.creationDate)
        #expect(dates == dates.sorted(), "identifier fetches came back unordered")
    }

    @Test(.enabled(if: LibraryFixtures.canSeedLibrary))
    func theFixtureAlbumIsAScope() async throws {
        _ = try await LibraryFixtures.asset(forFixture: "heic_gps_offset.heic")
        let options = PHFetchOptions()
        options.predicate = NSPredicate(format: "title = %@", LibraryFixtures.albumName)
        let album = try #require(
            PHAssetCollection.fetchAssetCollections(
                with: .album, subtype: .albumRegular, options: options
            ).firstObject)
        let found = LibraryScope.assets(in: .collection(localIdentifier: album.localIdentifier))
        #expect(found.count >= LibraryFixtures.allFixtures.count)
    }

    @Test(.enabled(if: LibraryFixtures.canSeedLibrary))
    func theInRangeCountIsAtLeastTheSelectionItCovers() async throws {
        let known = try await LibraryFixtures.asset(forFixture: "heic_gps_offset.heic")
        let selection = LibraryScope.assets(in: .identifiers([known.localIdentifier]))
        #expect(LibraryScope.inRangeCount(for: selection) >= selection.count)
    }
}
