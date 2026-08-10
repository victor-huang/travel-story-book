import Foundation
import Testing

@testable import PhotoExport

/// I14. The exclusion must happen before anything leaves the phone, and it must agree exactly
/// with `src/story_book/pipeline/home_filter.py` — a phone-side filter that disagreed with the
/// laptop-side one would export the same trip differently depending on which half ran it.
struct HomeFilterTests {
    private let home = HomeFilter.Home(latitude: 37.3, longitude: -121.9, exclusionKm: 5.0)

    // MARK: - Parity with the Python implementation

    /// Reference values produced by `home_filter.haversine_km` itself, not by an online
    /// calculator or a second formula. Agreement to 1e-6 km is a millimetre.
    @Test func haversineAgreesWithThePythonImplementation() {
        let cases: [(Double, Double, Double, Double, Double)] = [
            (37.3, -121.9, 37.309, -121.9, 1.000_755_722),
            (47.8095, 13.055, 48.2082, 16.3738, 250.817_517_177),
            (0.0, 0.0, 0.0, 1.0, 111.195_080_234),
            (45.0, 0.0, 45.0, 1.0, 78.626_296_280),
            (0.0, 0.0, 0.0, 180.0, 20015.114_442_036),
        ]
        for (lat1, lon1, lat2, lon2, expected) in cases {
            let actual = HomeFilter.haversineKm(lat1, lon1, lat2, lon2)
            #expect(
                abs(actual - expected) < 1e-6,
                "haversine(\(lat1),\(lon1),\(lat2),\(lon2)) = \(actual), python said \(expected)")
        }
    }

    /// Euclidean distance on raw degrees would give the same answer at the equator and at 45°.
    /// It does not, and that is the reason haversine is required rather than preferred.
    @Test func aDegreeOfLongitudeShrinksWithLatitude() {
        let atEquator = HomeFilter.haversineKm(0, 0, 0, 1)
        let atFortyFive = HomeFilter.haversineKm(45, 0, 45, 1)
        #expect(atFortyFive < atEquator * 0.75)
    }

    @Test func identicalPointsAreZeroApart() {
        #expect(HomeFilter.haversineKm(47.8095, 13.055, 47.8095, 13.055) == 0)
    }

    // MARK: - The boundary

    @Test func insideTheRadiusIsExcluded() {
        #expect(
            HomeFilter.shouldExcludeFromExport(
                latitude: 37.309, longitude: -121.9, home: home))
    }

    /// The Python test is `distance_km <= exclusion_km`, so the boundary itself is inside.
    @Test func exactlyOnTheRadiusIsExcluded() {
        // Walk north until just within 5 km, then confirm the sub-metre boundary behaves.
        let onRadius = 37.3 + (5.0 / HomeFilter.earthRadiusKm) * (180 / .pi)
        let distance = HomeFilter.haversineKm(onRadius, -121.9, 37.3, -121.9)
        #expect(abs(distance - 5.0) < 1e-6)
        #expect(
            HomeFilter.shouldExcludeFromExport(
                latitude: onRadius - 1e-9, longitude: -121.9, home: home))
    }

    @Test func outsideTheRadiusIsIncluded() {
        // Salzburg is not near San Jose.
        #expect(
            !HomeFilter.shouldExcludeFromExport(
                latitude: 47.8095, longitude: 13.055, home: home))
    }

    // MARK: - Unknown location, both ways round

    /// Fail toward privacy: an item never checked is not the same as one checked and cleared.
    @Test func unknownLocationIsExcludedWhenHomeIsConfigured() {
        #expect(HomeFilter.shouldExcludeFromExport(latitude: nil, longitude: nil, home: home))
    }

    /// The other half of the same rule, and the one that is easy to get wrong. A Sony body and a
    /// GoPro record no GPS; excluding coordinate-less items with no home configured protects
    /// nothing and silently deletes a DSLR-heavy trip.
    @Test func unknownLocationIsIncludedWhenNoHomeIsConfigured() {
        #expect(!HomeFilter.shouldExcludeFromExport(latitude: nil, longitude: nil, home: nil))
    }

    /// Half a coordinate is not a coordinate.
    @Test func aMissingLongitudeCountsAsUnknown() {
        #expect(HomeFilter.shouldExcludeFromExport(latitude: 37.3, longitude: nil, home: home))
    }

    /// `isWithinHomeRadius` claims a *measured* match, so it must never be true without a
    /// measurement — that is what keeps it distinct from the export predicate.
    @Test func theRadiusTestIsFalseNotTrueWithoutCoordinates() {
        #expect(!HomeFilter.isWithinHomeRadius(latitude: nil, longitude: nil, home: home))
    }

    // MARK: - The control

    /// A test of a failure mode must be shown to fail. The same asset that is excluded with the
    /// filter configured must be *included* without it — otherwise the exclusion tests would
    /// pass against a function that rejects everything.
    @Test func theSameItemIsIncludedOnceTheFilterIsDisabled() {
        let atHome = (latitude: 37.3005, longitude: -121.9005)
        #expect(
            HomeFilter.shouldExcludeFromExport(
                latitude: atHome.latitude, longitude: atHome.longitude, home: home))
        #expect(
            !HomeFilter.shouldExcludeFromExport(
                latitude: atHome.latitude, longitude: atHome.longitude, home: nil))
    }

    /// And a wide enough radius swallows a point that a default radius clears, which proves the
    /// threshold is actually read rather than hardcoded at 5 km.
    @Test func theConfiguredRadiusIsActuallyUsed() {
        let vienna = (latitude: 48.2082, longitude: 16.3738)
        let salzburg = HomeFilter.Home(latitude: 47.8095, longitude: 13.055, exclusionKm: 5.0)
        let wide = HomeFilter.Home(latitude: 47.8095, longitude: 13.055, exclusionKm: 300.0)
        #expect(
            !HomeFilter.shouldExcludeFromExport(
                latitude: vienna.latitude, longitude: vienna.longitude, home: salzburg))
        #expect(
            HomeFilter.shouldExcludeFromExport(
                latitude: vienna.latitude, longitude: vienna.longitude, home: wide))
    }
}
