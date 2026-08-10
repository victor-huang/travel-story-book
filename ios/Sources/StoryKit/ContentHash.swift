import Foundation

/// BLAKE2b-512, byte-for-byte identical to `hashlib.blake2b()` with default parameters:
/// 64-byte digest, unkeyed, hex-encoded to 128 characters (`src/story_book/pipeline/scan.py:45`).
/// Media is keyed by this hash everywhere, and `asset_id` is a prefix of it — so this digest
/// must match the Python side exactly or hash negotiation silently matches nothing.
///
/// CryptoKit and swift-crypto expose no BLAKE2b, so this is a vendored RFC 7693 implementation.
/// Streaming, so a 400 MB clip is never held in memory.
public struct ContentHash: Sendable {
    private static let blockSize = 128

    private static let iv: [UInt64] = [
        0x6a09_e667_f3bc_c908, 0xbb67_ae85_84ca_a73b,
        0x3c6e_f372_fe94_f82b, 0xa54f_f53a_5f1d_36f1,
        0x510e_527f_ade6_82d1, 0x9b05_688c_2b3e_6c1f,
        0x1f83_d9ab_fb41_bd6b, 0x5be0_cd19_137e_2179,
    ]

    private static let sigma: [[Int]] = [
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
        [11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4],
        [7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8],
        [9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13],
        [2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9],
        [12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11],
        [13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10],
        [6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5],
        [10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0],
    ]

    private var h: [UInt64]
    private var buffer: [UInt8]
    private var bytesCompressed: UInt64  // low word of t; a UInt64 overflows at 16 EiB
    private var finalized = false

    public init() {
        h = Self.iv
        // Parameter block word 0: digest_length=64, key_length=0, fanout=1, depth=1.
        h[0] ^= 0x0101_0040
        buffer = []
        buffer.reserveCapacity(Self.blockSize)
        bytesCompressed = 0
    }

    public mutating func update(_ data: some DataProtocol) {
        precondition(!finalized, "update after finalize")
        for region in data.regions {
            region.withUnsafeBytes { absorb($0) }
        }
    }

    private mutating func absorb(_ bytes: UnsafeRawBufferPointer) {
        var offset = 0
        while offset < bytes.count {
            // A full buffer is compressed only when another byte arrives, so the final block —
            // full or not — is always still buffered when finalize() sets the last-block flag.
            if buffer.count == Self.blockSize {
                bytesCompressed &+= UInt64(Self.blockSize)
                compress(buffer, isLast: false)
                buffer.removeAll(keepingCapacity: true)
            }
            let take = min(Self.blockSize - buffer.count, bytes.count - offset)
            buffer.append(
                contentsOf: UnsafeRawBufferPointer(rebasing: bytes[offset..<(offset + take)]))
            offset += take
        }
    }

    /// Hex digest, 128 lowercase characters.
    public mutating func finalize() -> String {
        precondition(!finalized, "finalize called twice")
        finalized = true
        bytesCompressed &+= UInt64(buffer.count)
        var block = buffer
        block.append(contentsOf: repeatElement(0, count: Self.blockSize - block.count))
        compress(block, isLast: true)
        var out = ""
        out.reserveCapacity(128)
        for word in h {
            var w = word.littleEndian
            withUnsafeBytes(of: &w) { raw in
                for byte in raw { out += String(format: "%02x", byte) }
            }
        }
        return out
    }

    // The hot loop: runs once per 128 bytes, so it avoids allocation and bounds checks.
    private mutating func compress(_ block: [UInt8], isLast: Bool) {
        let t = bytesCompressed
        block.withUnsafeBytes { raw in
            var m = (0 as UInt64, 0 as UInt64, 0 as UInt64, 0 as UInt64,
                     0 as UInt64, 0 as UInt64, 0 as UInt64, 0 as UInt64,
                     0 as UInt64, 0 as UInt64, 0 as UInt64, 0 as UInt64,
                     0 as UInt64, 0 as UInt64, 0 as UInt64, 0 as UInt64)
            withUnsafeMutableBytes(of: &m) { mb in
                let mp = mb.bindMemory(to: UInt64.self)
                for i in 0..<16 {
                    mp[i] = UInt64(littleEndian: raw.loadUnaligned(
                        fromByteOffset: i * 8, as: UInt64.self))
                }
                var v = (0 as UInt64, 0 as UInt64, 0 as UInt64, 0 as UInt64,
                         0 as UInt64, 0 as UInt64, 0 as UInt64, 0 as UInt64,
                         0 as UInt64, 0 as UInt64, 0 as UInt64, 0 as UInt64,
                         0 as UInt64, 0 as UInt64, 0 as UInt64, 0 as UInt64)
                withUnsafeMutableBytes(of: &v) { vb in
                    let vp = vb.bindMemory(to: UInt64.self)
                    h.withUnsafeBufferPointer { hp in
                        for i in 0..<8 { vp[i] = hp[i] }
                    }
                    Self.iv.withUnsafeBufferPointer { ivp in
                        for i in 0..<8 { vp[i + 8] = ivp[i] }
                    }
                    vp[12] ^= t
                    // vp[13] is the high word of t; unreachable below 2^64 input bytes.
                    if isLast { vp[14] = ~vp[14] }

                    func rotr(_ x: UInt64, _ n: UInt64) -> UInt64 {
                        (x >> n) | (x << (64 &- n))
                    }
                    func g(_ a: Int, _ b: Int, _ c: Int, _ d: Int, _ x: UInt64, _ y: UInt64) {
                        vp[a] = vp[a] &+ vp[b] &+ x
                        vp[d] = rotr(vp[d] ^ vp[a], 32)
                        vp[c] = vp[c] &+ vp[d]
                        vp[b] = rotr(vp[b] ^ vp[c], 24)
                        vp[a] = vp[a] &+ vp[b] &+ y
                        vp[d] = rotr(vp[d] ^ vp[a], 16)
                        vp[c] = vp[c] &+ vp[d]
                        vp[b] = rotr(vp[b] ^ vp[c], 63)
                    }

                    for round in 0..<12 {
                        Self.sigma[round % 10].withUnsafeBufferPointer { s in
                            g(0, 4, 8, 12, mp[s[0]], mp[s[1]])
                            g(1, 5, 9, 13, mp[s[2]], mp[s[3]])
                            g(2, 6, 10, 14, mp[s[4]], mp[s[5]])
                            g(3, 7, 11, 15, mp[s[6]], mp[s[7]])
                            g(0, 5, 10, 15, mp[s[8]], mp[s[9]])
                            g(1, 6, 11, 12, mp[s[10]], mp[s[11]])
                            g(2, 7, 8, 13, mp[s[12]], mp[s[13]])
                            g(3, 4, 9, 14, mp[s[14]], mp[s[15]])
                        }
                    }

                    h.withUnsafeMutableBufferPointer { hp in
                        for i in 0..<8 { hp[i] ^= vp[i] ^ vp[i + 8] }
                    }
                }
            }
        }
    }
}

extension ContentHash {
    /// One-shot digest of in-memory bytes.
    public static func hex(of data: some DataProtocol) -> String {
        var hasher = ContentHash()
        hasher.update(data)
        return hasher.finalize()
    }

    /// Chunked digest of a file. `chunkSize` defaults to 1 MiB so a 400 MB clip streams.
    public static func hex(of fileURL: URL, chunkSize: Int = 1 << 20) throws -> String {
        let handle = try FileHandle(forReadingFrom: fileURL)
        defer { try? handle.close() }
        var hasher = ContentHash()
        while let chunk = try handle.read(upToCount: chunkSize), !chunk.isEmpty {
            hasher.update(chunk)
        }
        return hasher.finalize()
    }
}
