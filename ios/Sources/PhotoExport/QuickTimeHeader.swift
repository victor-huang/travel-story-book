import Foundation

/// Copies the QuickTime *header* creation times from a source movie to an exported one.
///
/// `AVAssetExportSession` stamps `mvhd`/`tkhd`/`mdhd` with **the moment of export**, and those
/// are what a reader reports as `CreateDate`. That is wrong in both directions:
///
/// - A clip whose source header holds the `0` sentinel — genuinely unknown capture time — comes
///   out claiming it was shot at the instant of export. The pipeline believes it, places the clip
///   on that day, and builds an event around a date the traveller was not there. Measured: an
///   untimed fixture exported at 05:33 produced `taken_local = 2026-08-10T05:33:51` and a phantom
///   third day, where the original correctly yielded `None`.
/// - A clip that *does* carry a header time loses it, which is half of why a Photos-shaped `.mov`
///   has a `CreateDate` disagreeing with its `Keys:CreationDate`.
///
/// Copying the source's own value fixes both: unknown stays unknown, and known survives. This is
/// the file-format half of the standing rule that an artifact must not report a measurement
/// nobody made.
enum QuickTimeHeader {
    /// Byte ranges of the creation and modification fields inside one header atom.
    private struct HeaderTimes {
        let creationOffset: Int
        let width: Int  // 4 for version 0, 8 for version 1
    }

    private static let headerAtoms: Set<String> = ["mvhd", "tkhd", "mdhd"]
    private static let containerAtoms: Set<String> = ["moov", "trak", "mdia"]

    /// Read the movie header's creation and modification times, verbatim.
    static func movieCreationTimes(of url: URL) throws -> (creation: Data, modification: Data)? {
        let data = try Data(contentsOf: url)
        guard let found = locate(in: data, wanted: "mvhd").first else { return nil }
        let times = try headerTimes(in: data, atPayload: found)
        let creation = data.subdata(
            in: times.creationOffset..<(times.creationOffset + times.width))
        let modification = data.subdata(
            in: (times.creationOffset + times.width)..<(times.creationOffset + 2 * times.width))
        return (creation, modification)
    }

    /// Write `creation`/`modification` into every `mvhd`, `tkhd` and `mdhd` of `url`.
    ///
    /// All header atoms get the movie's value rather than a per-track one: the export's tracks do
    /// not correspond one-to-one with the source's (the spatial `apac` and six `mebx` tracks are
    /// dropped), so matching by index would assign times from the wrong track.
    static func setCreationTimes(
        in url: URL, creation: Data, modification: Data
    ) throws {
        var data = try Data(contentsOf: url)
        var patched = 0

        for name in headerAtoms {
            for payload in locate(in: data, wanted: name) {
                let times = try headerTimes(in: data, atPayload: payload)
                // Only copy when the widths agree; a version mismatch between source and export
                // is rare and silently truncating a 64-bit time into 32 bits would corrupt it.
                guard times.width == creation.count, times.width == modification.count else {
                    continue
                }
                let creationStart = times.creationOffset
                let modificationStart = creationStart + times.width
                let modificationEnd = modificationStart + times.width
                data.replaceSubrange(creationStart..<modificationStart, with: creation)
                data.replaceSubrange(modificationStart..<modificationEnd, with: modification)
                patched += 1
            }
        }

        guard patched > 0 else { return }
        try data.write(to: url, options: .atomic)
    }

    // MARK: - Atom walking

    /// Payload offsets of every atom named `wanted`, searching only through known containers so
    /// a byte sequence inside `mdat` cannot masquerade as a header.
    private static func locate(in data: Data, wanted: String) -> [Int] {
        var results: [Int] = []
        walk(data, from: 0, to: data.count) { name, payloadStart, _ in
            if name == wanted { results.append(payloadStart) }
        }
        return results
    }

    private static func walk(
        _ data: Data, from start: Int, to end: Int,
        _ visit: (String, Int, Int) -> Void
    ) {
        var cursor = start
        while cursor + 8 <= end {
            let size = Int(be32(data, cursor))
            guard let name = String(
                bytes: data[(cursor + 4)..<(cursor + 8)], encoding: .ascii)
            else { return }

            var atomSize = size
            var payloadStart = cursor + 8
            if size == 1 {
                guard cursor + 16 <= end else { return }
                atomSize = Int(be64(data, cursor + 8))
                payloadStart = cursor + 16
            } else if size == 0 {
                atomSize = end - cursor
            }
            guard atomSize >= 8, cursor + atomSize <= end else { return }

            visit(name, payloadStart, cursor + atomSize)
            if containerAtoms.contains(name) {
                walk(data, from: payloadStart, to: cursor + atomSize, visit)
            }
            cursor += atomSize
        }
    }

    private static func headerTimes(in data: Data, atPayload payload: Int) throws -> HeaderTimes {
        guard payload < data.count else { throw Failure.malformed }
        let version = data[payload]
        // version(1) + flags(3), then creation and modification.
        return HeaderTimes(creationOffset: payload + 4, width: version == 1 ? 8 : 4)
    }

    private static func be32(_ data: Data, _ offset: Int) -> UInt32 {
        (UInt32(data[offset]) << 24) | (UInt32(data[offset + 1]) << 16)
            | (UInt32(data[offset + 2]) << 8) | UInt32(data[offset + 3])
    }

    private static func be64(_ data: Data, _ offset: Int) -> UInt64 {
        var value: UInt64 = 0
        for i in 0..<8 { value = (value << 8) | UInt64(data[offset + i]) }
        return value
    }

    enum Failure: Error { case malformed }
}
