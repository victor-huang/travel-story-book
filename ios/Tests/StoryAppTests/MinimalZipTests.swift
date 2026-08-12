import Compression
import Foundation
import Testing

@testable import StoryApp

/// I26's own zip reader, exercised against zips this test builds and compresses itself — the
/// same round trip `delivery.py` (STORE for tiny files, DEFLATE otherwise via
/// `zipfile.ZIP_DEFLATED`) produces on the wire, minus the network.
struct MinimalZipTests {
    /// Builds a minimal, single-disk ZIP archive by hand: local headers, then a matching central
    /// directory, then one EOCD record. Mirrors the layout `MinimalZip.extractAll` parses, so a
    /// bug in one is very unlikely to cancel a bug in the other -- this is the same reasoning as
    /// pairing a measurement with a control that must differ from it.
    struct Builder {
        struct Entry { let name: String; let method: UInt16; let data: Data }

        static func zip(_ entries: [Entry]) -> Data {
            var body = Data()
            var centralDirectory = Data()
            var offsets: [Int] = []

            for entry in entries {
                offsets.append(body.count)
                let nameData = Data(entry.name.utf8)
                let compressed = compress(entry.data, method: entry.method)

                var local = Data()
                local.append(le32(0x04034b50))
                local.append(le16(20))  // version needed
                local.append(le16(0))  // flags
                local.append(le16(entry.method))
                local.append(le16(0))  // mod time
                local.append(le16(0))  // mod date
                local.append(le32(0))  // crc32, unchecked by the reader under test
                local.append(le32(UInt32(compressed.count)))
                local.append(le32(UInt32(entry.data.count)))
                local.append(le16(UInt16(nameData.count)))
                local.append(le16(0))  // extra length
                local.append(nameData)
                local.append(compressed)
                body.append(local)
            }

            for (index, entry) in entries.enumerated() {
                let nameData = Data(entry.name.utf8)
                let compressed = compress(entry.data, method: entry.method)
                var record = Data()
                record.append(le32(0x02014b50))
                record.append(le16(20))  // version made by
                record.append(le16(20))  // version needed
                record.append(le16(0))  // flags
                record.append(le16(entry.method))
                record.append(le16(0))
                record.append(le16(0))
                record.append(le32(0))  // crc32
                record.append(le32(UInt32(compressed.count)))
                record.append(le32(UInt32(entry.data.count)))
                record.append(le16(UInt16(nameData.count)))
                record.append(le16(0))  // extra length
                record.append(le16(0))  // comment length
                record.append(le16(0))  // disk number start
                record.append(le16(0))  // internal attrs
                record.append(le32(0))  // external attrs
                record.append(le32(UInt32(offsets[index])))
                record.append(nameData)
                centralDirectory.append(record)
            }

            var eocd = Data()
            eocd.append(le32(0x06054b50))
            eocd.append(le16(0))
            eocd.append(le16(0))
            eocd.append(le16(UInt16(entries.count)))
            eocd.append(le16(UInt16(entries.count)))
            eocd.append(le32(UInt32(centralDirectory.count)))
            eocd.append(le32(UInt32(body.count)))
            eocd.append(le16(0))  // comment length

            return body + centralDirectory + eocd
        }

        /// Method 0 (store) passes bytes through; method 8 (deflate) compresses with the same
        /// "ZLIB is actually raw DEFLATE" `Compression` algorithm `MinimalZip` decodes with, so
        /// the DEFLATE path is a real round trip, not an assumption about the format.
        private static func compress(_ data: Data, method: UInt16) -> Data {
            guard method == 8, !data.isEmpty else { return data }
            var output = [UInt8](repeating: 0, count: data.count + 64)
            let sourceBytes = [UInt8](data)
            let produced = output.withUnsafeMutableBufferPointer { outBuffer in
                sourceBytes.withUnsafeBufferPointer { inBuffer in
                    compression_encode_buffer(
                        outBuffer.baseAddress!, outBuffer.count,
                        inBuffer.baseAddress!, inBuffer.count,
                        nil, COMPRESSION_ZLIB)
                }
            }
            return Data(output.prefix(produced))
        }

        private static func le16(_ value: UInt16) -> Data {
            Data([UInt8(value & 0xff), UInt8((value >> 8) & 0xff)])
        }

        private static func le32(_ value: UInt32) -> Data {
            Data([
                UInt8(value & 0xff), UInt8((value >> 8) & 0xff),
                UInt8((value >> 16) & 0xff), UInt8((value >> 24) & 0xff),
            ])
        }
    }

    private func tempDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appending(
            path: "MinimalZipTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    @Test func aStoredEntryExtractsByteForByte() throws {
        let payload = Data("hello from a stored entry".utf8)
        let zip = Builder.zip([.init(name: "hello.txt", method: 0, data: payload)])
        let destination = try tempDirectory()

        try MinimalZip.extractAll(zip, to: destination)

        let written = try Data(contentsOf: destination.appending(path: "hello.txt"))
        #expect(written == payload)
    }

    /// The report is `zipfile.ZIP_DEFLATED` on the Python side (`delivery.py`) -- if only STORE
    /// round-tripped, every real report from the service would fail exactly where this test does
    /// not.
    @Test func aDeflatedEntryExtractsByteForByte() throws {
        // Repetitive enough that COMPRESSION_ZLIB actually shrinks it, so this exercises method 8
        // rather than accidentally falling back to a literal-only block.
        let payload = Data(String(repeating: "trip report ", count: 200).utf8)
        let zip = Builder.zip([.init(name: "report/index.html", method: 8, data: payload)])
        let destination = try tempDirectory()

        try MinimalZip.extractAll(zip, to: destination)

        let written = try Data(contentsOf: destination.appending(path: "report/index.html"))
        #expect(written == payload)
    }

    @Test func aNestedPathCreatesItsParentDirectories() throws {
        let payload = Data("leaflet".utf8)
        let zip = Builder.zip([.init(name: "report/vendor/leaflet.js", method: 0, data: payload)])
        let destination = try tempDirectory()

        try MinimalZip.extractAll(zip, to: destination)

        #expect(
            FileManager.default.fileExists(
                atPath: destination.appending(path: "report/vendor/leaflet.js").path))
    }

    @Test func multipleEntriesAllExtract() throws {
        let zip = Builder.zip([
            .init(name: "report/index.html", method: 8, data: Data("index".utf8)),
            .init(name: "report/style.css", method: 0, data: Data("css".utf8)),
            .init(name: "report/days/2026-07-18.html", method: 8, data: Data("day".utf8)),
        ])
        let destination = try tempDirectory()

        try MinimalZip.extractAll(zip, to: destination)

        #expect(try Data(contentsOf: destination.appending(path: "report/index.html")) == Data("index".utf8))
        #expect(try Data(contentsOf: destination.appending(path: "report/style.css")) == Data("css".utf8))
        #expect(
            try Data(contentsOf: destination.appending(path: "report/days/2026-07-18.html"))
                == Data("day".utf8))
    }

    /// The control: something that is not a zip at all must fail with a specific, named reason
    /// rather than silently producing an empty directory that a caller could mistake for success.
    @Test func dataWithNoEOCDSignatureIsRejected() throws {
        let destination = try tempDirectory()
        #expect(throws: MinimalZip.ZipError.eocdNotFound) {
            try MinimalZip.extractAll(Data("not a zip file at all, just text".utf8), to: destination)
        }
    }

    @Test func truncatedDataIsRejectedRatherThanCrashing() throws {
        let payload = Data("hello".utf8)
        let zip = Builder.zip([.init(name: "hello.txt", method: 0, data: payload)])
        let destination = try tempDirectory()

        #expect(throws: (any Error).self) {
            try MinimalZip.extractAll(zip.prefix(zip.count - 5), to: destination)
        }
    }
}
