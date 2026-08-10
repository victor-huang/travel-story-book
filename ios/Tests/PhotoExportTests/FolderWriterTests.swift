import Foundation
import Testing

@testable import PhotoExport
@testable import StoryKit

/// I15. The file source is enough to prove the folder shape, the sidecars, the ledger and the
/// resume path, so these run in CI (D12). The `PHAsset` half is exercised by the exporters'
/// own asset tests.
@Suite(.serialized)
struct FolderWriterTests {
    private func tempDir() throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "folder-writer-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private var stills: [ExportSource] {
        [
            .file(LibraryFixtures.url(forFixture: "iphone_heic_gps_offset.heic")),
            .file(LibraryFixtures.url(forFixture: "jpeg_gps_no_offset.jpg")),
        ]
    }

    @Test func writesEachSourceUnderItsOriginalFilename() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let writer = try FolderWriter(destination: dir)

        let summary = await writer.export(stills)

        #expect(summary.written == 2)
        #expect(summary.failed.isEmpty)
        #expect(
            FileManager.default.fileExists(
                atPath: dir.appending(path: "iphone_heic_gps_offset.heic").path))
        #expect(
            FileManager.default.fileExists(
                atPath: dir.appending(path: "jpeg_gps_no_offset.jpg").path))
    }

    @Test func writesASidecarBesideEachItem() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let writer = try FolderWriter(destination: dir)
        await writer.export(stills)

        let url = dir.appending(path: "iphone_heic_gps_offset.heic.sidecar.json")
        let sidecar = try Sidecar.decode(from: Data(contentsOf: url))
        #expect(sidecar.originalFilename == "iphone_heic_gps_offset.heic")
        #expect(sidecar.contentHash.count == 128)
        // A file source carries no PhotoKit truth, and the sidecar says so rather than
        // inventing a value the pipeline would then trust.
        #expect(sidecar.creation.source == .unavailable)
    }

    /// The hash is of the *exported* bytes, not the source's — that is the whole reason only the
    /// phone at export time can record the mapping (D6).
    @Test func ledgerRecordsTheHashOfTheExportedBytes() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let writer = try FolderWriter(destination: dir)
        await writer.export(stills)

        let ledger = await writer.ledgerSnapshot
        #expect(ledger.count == 2)

        let exported = dir.appending(path: "iphone_heic_gps_offset.heic")
        let exportedHash = try ContentHash.hex(of: exported)
        let entry = try #require(
            ledger.entries.first { $0.originalFilename == "iphone_heic_gps_offset.heic" })
        #expect(entry.contentHash == exportedHash)

        // Control: the source's own hash differs, so this cannot pass by hashing the input.
        let sourceHash = try ContentHash.hex(
            of: LibraryFixtures.url(forFixture: "iphone_heic_gps_offset.heic"))
        #expect(entry.contentHash != sourceHash)
    }

    // MARK: - Resume

    /// The acceptance criterion: restarting must not re-export what is already written.
    @Test func aSecondRunReExportsNothing() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        let first = try FolderWriter(destination: dir)
        let firstSummary = await first.export(stills)
        #expect(firstSummary.written == 2)

        // A fresh writer, as a relaunched app would be: state comes only from the ledger file.
        let second = try FolderWriter(destination: dir)
        let secondSummary = await second.export(stills)
        #expect(secondSummary.written == 0)
        #expect(secondSummary.skippedAlreadyExported == 2)
    }

    /// Interrupting after one item must cost at most that item — the ledger is saved per item,
    /// not at the end.
    @Test func anInterruptedRunResumesWithoutRedoingFinishedWork() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        let partial = try FolderWriter(destination: dir)
        await partial.export([stills[0]])

        // The ledger on disk already knows about item one, before item two was ever attempted.
        let onDisk = try ExportLedgerFile.load(
            from: dir.appending(path: ExportLedgerFile.defaultFileName))
        #expect(onDisk.count == 1)

        let resumed = try FolderWriter(destination: dir)
        let summary = await resumed.export(stills)
        #expect(summary.skippedAlreadyExported == 1)
        #expect(summary.written == 1)
    }

    /// A truncated file from a killed run is not in the ledger, so it must be redone. Treating
    /// file existence as completion would ship the truncation.
    @Test func aFilePresentButAbsentFromTheLedgerIsRewritten() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        let truncated = dir.appending(path: "jpeg_gps_no_offset.jpg")
        try Data([0xFF, 0xD8]).write(to: truncated)

        let writer = try FolderWriter(destination: dir)
        let summary = await writer.export([stills[1]])

        #expect(summary.written == 1)
        let size = try FileManager.default.attributesOfItem(atPath: truncated.path)[.size] as? Int
        #expect((size ?? 0) > 1000, "the truncated file was left in place")
    }

    // MARK: - Failure isolation

    /// One unreadable item must not cost the other 800.
    @Test func oneBadSourceDoesNotAbortTheRest() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let junk = dir.appending(path: "notes.txt")
        try "not media".write(to: junk, atomically: true, encoding: .utf8)

        let writer = try FolderWriter(destination: dir)
        let summary = await writer.export([.file(junk)] + stills)

        #expect(summary.written == 2)
        #expect(summary.failed.count == 1)
        #expect(summary.failed.first == "notes.txt")
    }

    @Test func progressReportsRealCountsNotAPercentage() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let writer = try FolderWriter(destination: dir)

        nonisolated(unsafe) var seen: [(Int, Int)] = []
        await writer.export(stills) { done, total in seen.append((done, total)) }

        #expect(seen.map(\.0) == [1, 2])
        #expect(seen.allSatisfy { $0.1 == 2 })
    }

    // MARK: - Video

    @Test func exportsClipsAlongsideStills() async throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let writer = try FolderWriter(destination: dir)

        let summary = await writer.export([
            .file(LibraryFixtures.url(forFixture: "clip_speech.mov")), stills[0],
        ])

        #expect(summary.written == 2)
        #expect(
            FileManager.default.fileExists(atPath: dir.appending(path: "clip_speech.mov").path))
    }
}
