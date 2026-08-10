import Foundation
import Testing

@testable import StoryKit

struct ExportLedgerTests {
    private func entry(
        id: String = "ABC-123/L0/001", hashByte: String = "0a", filename: String = "IMG_0001.heic"
    ) -> ExportLedger.Entry {
        .init(
            localIdentifier: id,
            contentHash: String(repeating: hashByte, count: 64),
            originalFilename: filename)
    }

    @Test func looksUpByLocalIdentifier() {
        var ledger = ExportLedger()
        ledger.record(entry())
        #expect(ledger.entry(forLocalIdentifier: "ABC-123/L0/001")?.originalFilename == "IMG_0001.heic")
        #expect(ledger.entry(forLocalIdentifier: "missing") == nil)
    }

    @Test func assetIdPrefixResolvesToItsEntry() {
        var ledger = ExportLedger()
        ledger.record(entry(id: "a", hashByte: "0a"))
        ledger.record(entry(id: "b", hashByte: "0b", filename: "IMG_0002.heic"))
        let matches = ledger.entries(matchingAssetId: "0a0a0a0a")
        #expect(matches.map(\.localIdentifier) == ["a"])
    }

    // Byte-identical duplicates share a hash; the prefix legitimately matches both.
    @Test func duplicateBytesYieldEveryMatchingEntry() {
        var ledger = ExportLedger()
        ledger.record(entry(id: "a", filename: "IMG_0001.heic"))
        ledger.record(entry(id: "b", filename: "IMG_0001 (1).heic"))
        #expect(ledger.entries(matchingAssetId: "0a0a").count == 2)
    }

    @Test func reRecordingAnAssetReplacesItsHash() {
        var ledger = ExportLedger()
        ledger.record(entry(hashByte: "0a"))
        ledger.record(entry(hashByte: "0b"))
        #expect(ledger.count == 1)
        #expect(ledger.entries(matchingAssetId: "0a").isEmpty)
    }

    @Test func survivesARelaunch() throws {
        let dir = FileManager.default.temporaryDirectory.appending(
            path: "ledger-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appending(path: ExportLedgerFile.defaultFileName)

        var ledger = ExportLedger()
        ledger.record(entry(id: "a", hashByte: "0a"))
        ledger.record(entry(id: "b", hashByte: "0b", filename: "IMG_0002.heic"))
        try ExportLedgerFile.save(ledger, to: url)

        // A fresh load from disk is the relaunch: nothing shared with the first instance.
        let reloaded = try ExportLedgerFile.load(from: url)
        #expect(reloaded == ledger)
    }

    @Test func missingFileLoadsAsEmptyLedger() throws {
        let url = FileManager.default.temporaryDirectory.appending(
            path: "never-written-\(UUID().uuidString).json")
        let ledger = try ExportLedgerFile.load(from: url)
        #expect(ledger.count == 0)
    }
}
