import Foundation
import Testing

@testable import StoryKit

/// The repo checkout, resolved from this file's path. Media fixtures live on the Python side
/// (`tests/fixtures/media/`) and are read in place rather than copied — two copies of one file
/// is one copy eventually wrong.
let repoRoot = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent()  // StoryKitTests
    .deletingLastPathComponent()  // Tests
    .deletingLastPathComponent()  // ios
    .deletingLastPathComponent()

let mediaFixtures = repoRoot.appending(path: "tests/fixtures/media")

struct ContentHashTests {
    // RFC 7693 appendix A: BLAKE2b-512("abc").
    @Test func matchesRFC7693Vector() {
        #expect(
            ContentHash.hex(of: Array("abc".utf8))
                == "ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d1"
                + "7d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923")
    }

    @Test func hashesEmptyInput() {
        #expect(
            ContentHash.hex(of: [UInt8]())
                == "786a02f742015903c6c6fd852552d272912f4740e15847618a86e217f71f5419"
                + "d25e1031afee585313896444934eb04b903a685b1448b755d56f701afe9be2ce")
    }

    // The last block is kept buffered until finalize; an input of exactly one block is the
    // boundary where getting that wrong flips the last-block flag onto the wrong compress.
    @Test func exactBlockBoundaryMatchesOneShot() {
        let block = [UInt8](repeating: 0xAB, count: 128)
        var chunked = ContentHash()
        chunked.update(block)
        var oneMore = ContentHash()
        oneMore.update(block + [0x01])
        #expect(chunked.finalize() != oneMore.finalize())
        #expect(ContentHash.hex(of: block) == chunkedHex(of: block, chunkSize: 1))
    }

    @Test func chunkingDoesNotChangeTheDigest() {
        let bytes = (0..<1000).map { UInt8($0 % 251) }
        let oneShot = ContentHash.hex(of: bytes)
        for chunkSize in [1, 7, 127, 128, 129, 999] {
            #expect(chunkedHex(of: bytes, chunkSize: chunkSize) == oneShot)
        }
    }

    /// The I02 gate: the same fixtures hashed by `hashlib.blake2b` on the Python side. The
    /// expected file is committed and re-verified against Python in CI (`ios.yml`), so this
    /// asserts cross-implementation agreement, not self-consistency.
    @Test func agreesWithPythonHashlibOnCommittedFixtures() throws {
        let expectedURL = try #require(
            Bundle.module.url(
                forResource: "expected_media_hashes", withExtension: "json",
                subdirectory: "Fixtures"))
        let expected = try JSONDecoder().decode(
            [String: String].self, from: Data(contentsOf: expectedURL))
        #expect(!expected.isEmpty)
        for (filename, hex) in expected {
            let fixture = mediaFixtures.appending(path: filename)
            #expect(
                FileManager.default.fileExists(atPath: fixture.path),
                "committed fixture missing: \(filename)")
            #expect(try ContentHash.hex(of: fixture) == hex, "digest mismatch for \(filename)")
            #expect(hex.count == 128)
        }
    }

    // The file API streams in 1 MiB chunks; the video fixture spans several, so this exercises
    // the chunked path against the same Python-computed digest.
    @Test func fileStreamingMatchesInMemory() throws {
        let fixture = mediaFixtures.appending(path: "clip_speech.mov")
        let data = try Data(contentsOf: fixture)
        #expect(try ContentHash.hex(of: fixture, chunkSize: 4096) == ContentHash.hex(of: data))
    }

    private func chunkedHex(of bytes: [UInt8], chunkSize: Int) -> String {
        var hasher = ContentHash()
        var offset = 0
        while offset < bytes.count {
            let end = min(offset + chunkSize, bytes.count)
            hasher.update(Array(bytes[offset..<end]))
            offset = end
        }
        return hasher.finalize()
    }
}
