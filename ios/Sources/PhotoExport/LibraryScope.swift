import Foundation
import Photos

/// Picking the assets a trip is made of, and warning when the pick has been thinned too far.
///
/// **Full library read authorization, not `PHPickerViewController` alone.** A picker hands back
/// files; the pipeline wants properties of the *asset in the library* — `creationDate`,
/// `location`, burst identifiers, Live Photo pairing — and those do not survive the handover.
/// The picker is the privacy-friendlier control and it is the wrong tool here, which is a real
/// cost of this design and is why the app asks for the larger permission and says why.
public enum LibraryScope {
    // MARK: - Authorization

    /// Read authorization. `.limited` is a real answer, not a failure: iOS lets the user grant a
    /// subset, and a subset still exports — it just cannot see the assets it was not shown, so
    /// the cull check below will have nothing to compare against.
    public static func requestReadAuthorization() async -> PHAuthorizationStatus {
        let current = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        // Only ask when the answer is genuinely unknown. Asking while holding add-only access
        // raises an "additional access" upgrade dialog, which nothing in-process can dismiss.
        guard current == .notDetermined else { return current }
        return await PHPhotoLibrary.requestAuthorization(for: .readWrite)
    }

    public static var isAuthorized: Bool {
        let status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        return status == .authorized || status == .limited
    }

    // MARK: - Scopes

    /// The three low-effort ways to say "this trip".
    ///
    /// All three are *set* selections, never a filtered stream: event detection segments on time
    /// and location gaps, and dedup clusters bursts. Both read everything, so a scope that
    /// quietly dropped the dull frames between two landmarks would produce gappy events and a
    /// selection with nothing to select from.
    public enum Scope: Sendable {
        /// Everything captured between two instants, inclusive of both ends.
        case dateRange(from: Date, to: Date)
        /// An album, or a Photos "Trip" memory — both are `PHAssetCollection`s.
        case collection(localIdentifier: String)
        /// What a multi-select drag across the grid produced.
        case identifiers([String])
    }

    /// Assets in capture order. Ordering is by `creationDate` because everything downstream is:
    /// the pipeline orders by UTC and splits days by local time, and handing it an arbitrary
    /// order would make the first build's day boundaries depend on fetch order.
    public static func assets(in scope: Scope) -> [PHAsset] {
        switch scope {
        case let .dateRange(from, to):
            let options = fetchOptions()
            options.predicate = NSPredicate(
                format: "creationDate >= %@ AND creationDate <= %@",
                from as NSDate, to as NSDate)
            return materialize(PHAsset.fetchAssets(with: options))

        case let .collection(localIdentifier):
            let collections = PHAssetCollection.fetchAssetCollections(
                withLocalIdentifiers: [localIdentifier], options: nil)
            guard let collection = collections.firstObject else { return [] }
            return materialize(PHAsset.fetchAssets(in: collection, options: fetchOptions()))

        case let .identifiers(identifiers):
            guard !identifiers.isEmpty else { return [] }
            // `fetchAssets(withLocalIdentifiers:)` ignores `sortDescriptors`, so the sort is
            // applied here rather than requested and quietly not delivered.
            let fetched = materialize(
                PHAsset.fetchAssets(withLocalIdentifiers: identifiers, options: nil))
            return fetched.sorted {
                ($0.creationDate ?? .distantPast) < ($1.creationDate ?? .distantPast)
            }
        }
    }

    /// Images and videos, oldest first. Audio and whatever Apple adds next are not media this
    /// pipeline imports, and including them would put items in the count that can never be
    /// exported.
    static func fetchOptions() -> PHFetchOptions {
        let options = PHFetchOptions()
        options.predicate = NSPredicate(
            format: "mediaType == %d OR mediaType == %d",
            PHAssetMediaType.image.rawValue, PHAssetMediaType.video.rawValue)
        options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: true)]
        return options
    }

    static func materialize(_ result: PHFetchResult<PHAsset>) -> [PHAsset] {
        var assets: [PHAsset] = []
        assets.reserveCapacity(result.count)
        result.enumerateObjects { asset, _, _ in assets.append(asset) }
        return assets
    }

    // MARK: - The cull check

    /// How many assets exist in the library between the first and last item of a selection.
    ///
    /// This is the denominator of the cull check: not "how many did you pick" but "how many were
    /// there to pick from, over the days you picked across".
    public static func inRangeCount(for selection: [PHAsset]) -> Int {
        let dates = selection.compactMap(\.creationDate).sorted()
        guard let first = dates.first, let last = dates.last else { return 0 }
        let options = fetchOptions()
        options.predicate = NSPredicate(
            format: "creationDate >= %@ AND creationDate <= %@ AND (mediaType == %d OR mediaType == %d)",
            first as NSDate, last as NSDate,
            PHAssetMediaType.image.rawValue, PHAssetMediaType.video.rawValue)
        return PHAsset.fetchAssets(with: options).count
    }

    public static func cullCheck(for selection: [PHAsset]) -> CullCheck.Reading {
        CullCheck.reading(selected: selection.count, inRange: inRangeCount(for: selection))
    }
}

/// Warns when a selection has been thinned to the point where the pipeline will do worse work.
///
/// The traveller who has already deleted down to favourites has done the tiring part of the job
/// *and* got a worse result: event detection reads the stream, dedup needs the alternates, and a
/// selection with nothing to choose between produces gappy events and no alternates strip. So the
/// advice is always the same — when in doubt, include it — and it is **advice**. Nothing here
/// blocks an export.
public enum CullCheck {
    /// Where the threshold is has **not been settled** — open question 5 says to set it by
    /// watching real selections rather than by guessing, and no real selections have been watched
    /// yet. What *is* settled is the pair of anchors the human gave: 60 of 800 clearly warrants
    /// the nudge, 600 of 800 clearly does not.
    ///
    /// So the admissible band is `(0.075, 0.75]` and this is a placeholder sitting in it, not a
    /// measurement. `LibraryScope` and the UI both take it as a parameter, so replacing it when
    /// there is evidence changes one line and no behaviour anyone depends on. A test asserts the
    /// two anchors stay separated for *every* threshold in the band, so the band is the claim and
    /// this number is not.
    public static let provisionalNudgeBelow = 0.5

    /// The anchors, kept next to the threshold they constrain so a future change can be checked
    /// against them.
    public static let anchorNudge = (selected: 60, inRange: 800)
    public static let anchorFine = (selected: 600, inRange: 800)

    public enum Verdict: Sendable, Equatable {
        case nudge
        case fine
        /// Nothing to compare against — an empty selection, or a library that returned no
        /// in-range count (a `.limited` grant sees only what it was shown). **Not** `fine`: a
        /// reading that could not be taken must not read like a reading that came back clean.
        case noEvidence
    }

    public struct Reading: Sendable, Equatable {
        public let selected: Int
        public let inRange: Int
        public let verdict: Verdict

        /// `nil` when there was nothing to divide by. A zero here would compare cleanly against
        /// a real, genuinely-zero fraction and hide the difference.
        public var keptFraction: Double? {
            inRange > 0 ? Double(selected) / Double(inRange) : nil
        }
    }

    public static func reading(
        selected: Int, inRange: Int, nudgeBelow: Double = provisionalNudgeBelow
    ) -> Reading {
        guard selected > 0, inRange > 0 else {
            return Reading(selected: selected, inRange: inRange, verdict: .noEvidence)
        }
        let kept = Double(selected) / Double(inRange)
        return Reading(
            selected: selected, inRange: inRange, verdict: kept < nudgeBelow ? .nudge : .fine)
    }
}
