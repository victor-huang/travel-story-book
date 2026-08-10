import Foundation

#if canImport(Photos)
    import Photos
#endif

/// What an exporter reads from (tracker D12).
///
/// `PHAsset` is the production path and the only source of `creationDate`, `location`, burst
/// identifiers and Live Photo pairing. A plain file URL is the same bytes without a library:
/// it is what lets the metadata-preserving core be tested in CI, where no photo library exists
/// and PhotoKit authorization is unobtainable (D11), and it is how the I16 parity harness
/// drives both halves through one API.
public enum ExportSource: Sendable {
    /// A file on disk. Its own name is the original filename.
    case file(URL)

    #if canImport(Photos)
        /// An asset in the photo library. Which of its resources gets exported is
        /// `ResourceSelection`'s decision.
        case asset(PHAsset)
    #endif
}

/// Bytes to export, plus the filename they must keep.
///
/// `overrides.toml` addresses media by filename (`src/story_book/overrides.py:205`), so
/// `IMG_1815.mov` staying `IMG_1815.mov` is what lets a correction made on the laptop carry
/// across to the phone.
public struct ResolvedResource: Sendable, Equatable {
    public let originalFilename: String
    public let kind: Kind
    /// Why this resource was chosen, for the test that has to name it and for diagnostics.
    public let reason: String

    public enum Kind: String, Sendable, Equatable {
        case still
        case video
    }

    public init(originalFilename: String, kind: Kind, reason: String) {
        self.originalFilename = originalFilename
        self.kind = kind
        self.reason = reason
    }
}
