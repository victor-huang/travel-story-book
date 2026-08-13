#if os(iOS)

    import Photos
    import PhotoExport
    import SwiftUI

    /// I17. The one screen that makes M0 something a human can run: pick a scope, see what the cull
    /// check thinks, export, hand the folder off.
    ///
    /// **This view owns no logic.** Scope selection, the nudge, the privacy filter, the exporters and
    /// the ledger are all built and tested in `PhotoExport`; the screen only calls them and shows what
    /// they return. If anything here starts deciding something, it belongs one layer down. I23 replaces
    /// the shell around it.
    @available(iOS 17.0, *)
    public struct ExportScreen: View {
        @State private var model = ExportModel()

        public init() {}

        @State private var showingSelectionGrid = false

        public var body: some View {
            NavigationStack {
                Form {
                    scopeSection
                    privacySection
                    if !model.assets.isEmpty { selectionSection }
                    exportSection
                    if let message = model.errorMessage { errorSection(message) }
                    if let summary = model.summary { resultSection(summary) }
                }
                .navigationTitle("Export a trip")
                .sheet(isPresented: $showingSelectionGrid) {
                    NavigationStack {
                        SelectionGridScreen(
                            assets: model.assets, excludedAssetIDs: $model.excludedAssetIDs
                        )
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) {
                                Button("Done") { showingSelectionGrid = false }
                            }
                        }
                    }
                }
            }
            .task { await model.requestAccess() }
        }

        private var scopeSection: some View {
            Section("Scope") {
                if model.authorization != .authorized {
                    Text(
                        model.authorization == .limited
                            ? "Limited access: only the photos you granted are visible, and the cull "
                                + "check cannot see the rest."
                            : "Waiting for photo library access."
                    )
                    .font(.footnote)
                    .foregroundStyle(.orange)
                }

                Picker("Choose by", selection: $model.scopeKind) {
                    ForEach(ExportModel.ScopeKind.allCases, id: \.self) { kind in
                        Text(kind.label).tag(kind)
                    }
                }

                switch model.scopeKind {
                case .dateRange:
                    DatePicker("From", selection: $model.start, displayedComponents: .date)
                    DatePicker("To", selection: $model.end, displayedComponents: .date)
                case .album:
                    if model.albums.isEmpty {
                        Text("No albums in this library").foregroundStyle(.secondary)
                    } else {
                        Picker("Album", selection: $model.albumIndex) {
                            ForEach(model.albums.indices, id: \.self) { index in
                                Text(model.albums[index].localizedTitle ?? "Untitled").tag(index)
                            }
                        }
                    }
                }

                Button("Find media") { model.findMedia() }
                    .disabled(!model.canRead)
            }
        }

        /// The exclusion has to be a decision rather than an oversight, so it sits on the screen the
        /// export is started from — not in a settings pane nobody opens.
        private var privacySection: some View {
            Section {
                Toggle("Exclude media shot near home", isOn: $model.homeEnabled)
                if model.homeEnabled {
                    HStack {
                        Text("Latitude")
                        Spacer()
                        TextField("47.7949", value: $model.homeLatitude, format: .number)
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.numbersAndPunctuation)
                    }
                    HStack {
                        Text("Longitude")
                        Spacer()
                        TextField("13.0475", value: $model.homeLongitude, format: .number)
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.numbersAndPunctuation)
                    }
                    Stepper(
                        "Within \(model.exclusionKm, specifier: "%.0f") km",
                        value: $model.exclusionKm, in: 1...50)
                }
            } header: {
                Text("Privacy")
            } footer: {
                Text(
                    model.homeEnabled
                        ? "Excluded media is never written to the folder and never leaves the phone. "
                            + "Anything with no location is excluded too, because it cannot be checked."
                        : "With no home set nothing is excluded — including media shot at home."
                )
            }
        }

        private var selectionSection: some View {
            Section("Selection") {
                if model.excludedAssetIDs.isEmpty {
                    LabeledContent("Found", value: "\(model.assets.count) items")
                } else {
                    // Once anything has been deselected, "found" alone would hide the number
                    // that actually matters -- how many will export -- so both are shown rather
                    // than silently picking one (I18's brief, on the same ambiguity).
                    LabeledContent(
                        "Found",
                        value: "\(model.assets.count) items, \(model.includedAssets.count) selected"
                    )
                }
                Button("Review and deselect") { showingSelectionGrid = true }
                    .font(.footnote)
                exclusionPreview

                if let reading = model.cullReading {
                    switch reading.verdict {
                    case .noEvidence:
                        // A reading that could not be taken must not read like one that came back
                        // clean, so this says so rather than showing a reassuring number.
                        Text("Not enough information to judge the selection.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    case .fine:
                        LabeledContent("In range", value: "\(reading.inRange) items")
                    case .nudge:
                        LabeledContent("In range", value: "\(reading.inRange) items")
                        Text(
                            "This looks thinned: \(reading.selected) of \(reading.inRange) in the "
                                + "same span. Event detection reads the whole stream and dedup needs "
                                + "the alternates, so a pruned set gives gappy events and nothing to "
                                + "choose between. When in doubt, include it — but this is only advice."
                        )
                        .font(.footnote)
                        .foregroundStyle(.orange)
                    }
                }
            }
        }

        /// What the privacy filter would remove, before the export runs. Previewed against
        /// `includedAssets` rather than `assets` -- a grid deselection already means "do not
        /// send this", so the number that matters here is what home-exclusion would additionally
        /// remove from what is actually about to be exported, not from everything the scope found.
        @ViewBuilder
        private var exclusionPreview: some View {
            if let preview = model.exclusionPreview {
                let excluded = preview.excludedCount
                let included = model.includedAssets.count
                if excluded == included {
                    Text(
                        "Every item would be excluded — \(preview.excludedNearHome.count) near home "
                            + "and \(preview.excludedUnknownLocation.count) with no location. The "
                            + "export would write nothing. Move the home coordinates, shrink the "
                            + "radius, or turn the exclusion off."
                    )
                    .font(.footnote)
                    .foregroundStyle(.red)
                } else if excluded > 0 {
                    LabeledContent(
                        "Would be excluded",
                        value: "\(excluded) of \(included)")
                    Text(
                        "\(preview.excludedNearHome.count) near home, "
                            + "\(preview.excludedUnknownLocation.count) with no location."
                    )
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }
            }
        }

        private var exportSection: some View {
            Section {
                Button(model.isExporting ? "Exporting…" : "Export to a folder") {
                    Task { await model.export() }
                }
                .disabled(model.includedAssets.isEmpty || model.isExporting)

                if model.progressTotal > 0 {
                    // Real counts, straight from the writer. Never a fabricated percentage.
                    ProgressView(
                        value: Double(model.progressDone), total: Double(model.progressTotal)
                    ) {
                        Text("\(model.progressDone) of \(model.progressTotal)")
                    }
                }
            } footer: {
                Text(
                    "Writes 1080px stills and 1080p clips with their capture metadata intact, keeping "
                        + "the original filenames. Nothing is uploaded. Re-running skips what is "
                        + "already exported."
                )
            }
        }

        private func errorSection(_ message: String) -> some View {
            Section("Could not export") {
                Text(message).font(.footnote).foregroundStyle(.red).textSelection(.enabled)
            }
        }

        private func resultSection(_ summary: FolderWriter.Summary) -> some View {
            Section("Result") {
                LabeledContent("Written", value: "\(summary.written)")
                if summary.skippedAlreadyExported > 0 {
                    LabeledContent("Already exported", value: "\(summary.skippedAlreadyExported)")
                }
                if summary.excludedNearHome > 0 {
                    LabeledContent("Excluded near home", value: "\(summary.excludedNearHome)")
                }
                if summary.excludedUnknownLocation > 0 {
                    // Counted apart from near-home because these are ambiguous, not private: the
                    // traveller can get them back.
                    LabeledContent(
                        "Excluded, no location", value: "\(summary.excludedUnknownLocation)")
                }
                if !summary.failed.isEmpty {
                    LabeledContent("Failed", value: "\(summary.failed.count)")
                        .foregroundStyle(.red)
                    ForEach(summary.failed.prefix(5), id: \.self) { name in
                        Text(name).font(.caption2).foregroundStyle(.red)
                    }
                }
                if let folder = model.folder {
                    ShareLink(item: folder) { Text("Send the folder") }
                    Text(folder.path)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
        }
    }

    /// The screen's state. Calls into `PhotoExport` and holds what it returns; decides nothing.
    @available(iOS 17.0, *)
    @MainActor
    @Observable
    final class ExportModel {
        enum ScopeKind: CaseIterable, Hashable {
            case dateRange, album

            var label: String {
                switch self {
                case .dateRange: return "Dates"
                case .album: return "Album"
                }
            }
        }

        var authorization: PHAuthorizationStatus = .notDetermined
        var scopeKind: ScopeKind = .dateRange
        var start = Calendar.current.date(byAdding: .day, value: -30, to: .now) ?? .now
        var end = Date.now
        var albums: [PHAssetCollection] = []
        var albumIndex = 0

        var homeEnabled = false
        var homeLatitude = 0.0
        var homeLongitude = 0.0
        var exclusionKm = 5.0

        var assets: [PHAsset] = []
        var cullReading: CullCheck.Reading?
        /// What the grid (`SelectionGridScreen`) has deselected, by `localIdentifier` -- excluded
        /// rather than included, so an asset a widened scope newly finds defaults to included
        /// without this set needing to be backfilled.
        var excludedAssetIDs: Set<String> = []

        var isExporting = false
        var progressDone = 0
        var progressTotal = 0
        var summary: FolderWriter.Summary?
        var folder: URL?
        /// Kept apart from `summary`: a run that could not start is not a run that found nothing.
        var errorMessage: String?

        var canRead: Bool { authorization == .authorized || authorization == .limited }

        /// What the scope found, minus what the grid has deselected. This, not `assets`, is what
        /// `export()` sends -- a grid deselection is a decision made before export starts, the
        /// same standing as the home-exclusion toggle.
        var includedAssets: [PHAsset] {
            assets.filter { !excludedAssetIDs.contains($0.localIdentifier) }
        }

        /// What the home filter *would* do, computed from the same `HomeFilter.partition` the export
        /// uses, and against `includedAssets` -- see `exclusionPreview` in `ExportScreen` for why.
        /// Shown before exporting, because finding out afterwards means the work was wasted -- the
        /// same reason the cull check warns up front.
        var exclusionPreview: HomeFilter.Partition? {
            guard homeEnabled, !includedAssets.isEmpty else { return nil }
            return HomeFilter.partition(includedAssets, home: currentHome)
        }

        var currentHome: HomeFilter.Home? {
            homeEnabled
                ? HomeFilter.Home(
                    latitude: homeLatitude, longitude: homeLongitude, exclusionKm: exclusionKm)
                : nil
        }

        func requestAccess() async {
            authorization = await LibraryScope.requestReadAuthorization()
            if canRead { loadAlbums() }
        }

        private func loadAlbums() {
            let fetched = PHAssetCollection.fetchAssetCollections(
                with: .album, subtype: .any, options: nil)
            var found: [PHAssetCollection] = []
            fetched.enumerateObjects { collection, _, _ in found.append(collection) }
            albums = found
        }

        func findMedia() {
            let scope: LibraryScope.Scope
            switch scopeKind {
            case .dateRange:
                scope = .dateRange(from: startOfDay(start), to: endOfDay(end))
            case .album:
                guard albumIndex < albums.count else { return }
                scope = .collection(localIdentifier: albums[albumIndex].localIdentifier)
            }
            assets = LibraryScope.assets(in: scope)
            cullReading = LibraryScope.cullCheck(for: assets)
            // A fresh search must not inherit a stale exclusion set left over from a previous
            // scope -- an identifier a widened or changed scope no longer contains is dead
            // weight at best, and at worst silently excludes an unrelated asset that happens to
            // reuse it.
            excludedAssetIDs = []
            summary = nil
            folder = nil
            errorMessage = nil
            progressDone = 0
            progressTotal = 0
        }

        func export() async {
            isExporting = true
            progressDone = 0
            let sources = includedAssets.map { ExportSource.asset($0) }
            progressTotal = sources.count
            defer { isExporting = false }

            // Documents, so the folder is reachable from the Files app and from a share sheet.
            let destination = URL.documentsDirectory.appending(path: "trip-\(Self.stamp())")
            let home = currentHome

            do {
                let writer = try FolderWriter(destination: destination, home: home)
                let result = await writer.export(sources) { done, total in
                    Task { @MainActor in
                        self.progressDone = done
                        self.progressTotal = total
                    }
                }
                summary = result
                // An export that wrote nothing should not leave a directory behind for the traveller
                // to find and wonder about.
                if result.written == 0, result.skippedAlreadyExported == 0 {
                    try? FileManager.default.removeItem(at: destination)
                    folder = nil
                } else {
                    folder = destination
                }
            } catch {
                errorMessage = "\(error)"
            }
        }

        private static func stamp() -> String {
            let formatter = DateFormatter()
            formatter.dateFormat = "yyyyMMdd-HHmmss"
            return formatter.string(from: .now)
        }

        private func startOfDay(_ date: Date) -> Date {
            Calendar.current.startOfDay(for: date)
        }

        private func endOfDay(_ date: Date) -> Date {
            Calendar.current.date(byAdding: .day, value: 1, to: startOfDay(date)) ?? date
        }
    }

#endif
