import CoreLocation
import Foundation
import Photos

/// Keeps media shot near the traveller's home out of the export.
///
/// On the laptop `home_filter` runs as a pipeline stage, which is fine when ingestion is a local
/// folder. On a phone it is useless there: by the time the pipeline runs, the bytes have already
/// left the device. **So the exclusion happens before export, and excluded media is never
/// written, never hashed into the ledger, and never uploaded.**
///
/// This mirrors `src/story_book/pipeline/home_filter.py` deliberately and exactly — same
/// haversine, same Earth radius, same inclusive boundary, same treatment of unknown locations —
/// because a phone-side filter that disagreed with the laptop-side one would make the same trip
/// export differently depending on which half ran it.
public enum HomeFilter {
    /// Mean Earth radius (IUGG authalic mean). Matches `home_filter.EARTH_RADIUS_KM`.
    public static let earthRadiusKm = 6371.0088

    /// Great-circle distance in kilometres.
    ///
    /// Euclidean distance on raw degrees is wrong by a factor that shrinks with latitude — a
    /// degree of longitude is ~111 km at the equator, ~79 km at 45°, and 0 at the poles — so the
    /// sphere has to be accounted for.
    public static func haversineKm(
        _ lat1: Double, _ lon1: Double, _ lat2: Double, _ lon2: Double
    ) -> Double {
        let phi1 = lat1 * .pi / 180
        let phi2 = lat2 * .pi / 180
        let dPhi = (lat2 - lat1) * .pi / 180
        let dLambda = (lon2 - lon1) * .pi / 180
        let a =
            sin(dPhi / 2) * sin(dPhi / 2)
            + cos(phi1) * cos(phi2) * sin(dLambda / 2) * sin(dLambda / 2)
        return 2 * earthRadiusKm * asin(min(1, sqrt(a)))
    }

    /// Where the traveller lives. `exclusionKm` defaults to 5.0, matching `HomeLocation`.
    public struct Home: Sendable, Equatable {
        public let latitude: Double
        public let longitude: Double
        public let exclusionKm: Double

        public init(latitude: Double, longitude: Double, exclusionKm: Double = 5.0) {
            self.latitude = latitude
            self.longitude = longitude
            self.exclusionKm = exclusionKm
        }
    }

    /// The distance test. **Inclusive**: exactly `exclusionKm` away is inside.
    ///
    /// `false` — never `true` — when there are no coordinates. There is nothing to measure, and
    /// this flag claims a *measured* match; how exports must treat the unmeasured case is
    /// `shouldExcludeFromExport`'s job.
    public static func isWithinHomeRadius(
        latitude: Double?, longitude: Double?, home: Home
    ) -> Bool {
        guard let latitude, let longitude else { return false }
        return haversineKm(latitude, longitude, home.latitude, home.longitude) <= home.exclusionKm
    }

    /// The predicate every export must call. Two cases exclude:
    ///
    /// - the item is within the radius, or
    /// - **its location is unknown and a home is configured** — fail toward privacy, because an
    ///   item never checked is not the same as one checked and cleared.
    ///
    /// The `home` argument is what makes the second case correct. Excluding coordinate-less
    /// items unconditionally protects nothing when no home is set and quietly deletes real
    /// content: a Sony body and a GoPro record no GPS, so a DSLR-heavy trip would lose those
    /// photographs with no error. Guarding a home that was never configured is not caution, it
    /// is data loss.
    public static func shouldExcludeFromExport(
        latitude: Double?, longitude: Double?, home: Home?
    ) -> Bool {
        guard let home else { return false }
        if latitude == nil || longitude == nil { return true }
        return isWithinHomeRadius(latitude: latitude, longitude: longitude, home: home)
    }

    // MARK: - Assets

    public static func shouldExcludeFromExport(_ asset: PHAsset, home: Home?) -> Bool {
        let coordinate = asset.location?.coordinate
        return shouldExcludeFromExport(
            latitude: coordinate?.latitude, longitude: coordinate?.longitude, home: home)
    }

    /// What a caller needs to both act and report.
    ///
    /// `excludedUnknownLocation` is counted separately on purpose: those items are ambiguous
    /// rather than private, and the user should be able to see that widening the interpolation
    /// window or adding an override would bring them back — rather than losing photographs
    /// silently. An artifact never overstates its contents, and neither does a filter.
    public struct Partition: Sendable {
        public let included: [PHAsset]
        public let excludedNearHome: [PHAsset]
        public let excludedUnknownLocation: [PHAsset]

        public var excludedCount: Int {
            excludedNearHome.count + excludedUnknownLocation.count
        }
        /// True when a home was configured at all. `privacy.home_configured` in `trip.json`
        /// exists so a filter that did not run cannot read as a filter that found nothing.
        public let homeConfigured: Bool
    }

    public static func partition(_ assets: [PHAsset], home: Home?) -> Partition {
        var included: [PHAsset] = []
        var nearHome: [PHAsset] = []
        var unknown: [PHAsset] = []

        for asset in assets {
            guard let home else {
                included.append(asset)
                continue
            }
            guard let coordinate = asset.location?.coordinate else {
                unknown.append(asset)
                continue
            }
            if isWithinHomeRadius(
                latitude: coordinate.latitude, longitude: coordinate.longitude, home: home)
            {
                nearHome.append(asset)
            } else {
                included.append(asset)
            }
        }

        return Partition(
            included: included,
            excludedNearHome: nearHome,
            excludedUnknownLocation: unknown,
            homeConfigured: home != nil)
    }
}
