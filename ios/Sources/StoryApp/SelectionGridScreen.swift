#if os(iOS)

    import Photos
    import PhotoExport
    import SwiftUI
    import UIKit

    /// I18. What a scope found, one thumbnail per asset, with a tap to deselect before export.
    ///
    /// **This view owns no export logic**, matching `ExportScreen`'s own rule: it renders exactly
    /// the `[PHAsset]` array `ExportModel.assets` already holds and writes to the exclusion set
    /// `ExportModel` owns — it does not requery the library (that stays `LibraryScope`'s job) and
    /// it does not decide what gets exported (that stays `ExportModel.export()`'s job, which reads
    /// `includedAssets`). A `Binding<Set<String>>` rather than a second `@Observable` model, for
    /// the same reason `ExportScreen`'s privacy section binds straight into `ExportModel` fields:
    /// one state owner per screen tree, not two competing ones.
    @available(iOS 17.0, *)
    public struct SelectionGridScreen: View {
        let assets: [PHAsset]
        @Binding var excludedAssetIDs: Set<String>

        @State private var loader = ThumbnailLoader()
        @State private var detail: DetailTarget?

        private let columns = [GridItem(.adaptive(minimum: 92), spacing: 2)]

        public init(assets: [PHAsset], excludedAssetIDs: Binding<Set<String>>) {
            self.assets = assets
            self._excludedAssetIDs = excludedAssetIDs
        }

        private var selectedCount: Int { assets.count - excludedAssetIDs.count }

        public var body: some View {
            ScrollView {
                LazyVGrid(columns: columns, spacing: 2) {
                    ForEach(assets, id: \.localIdentifier) { asset in
                        ThumbnailCell(
                            asset: asset, loader: loader,
                            isExcluded: excludedAssetIDs.contains(asset.localIdentifier)
                        )
                        .onTapGesture { toggle(asset) }
                        // A tap toggles selection, matching the system Photos picker, so a second
                        // gesture is needed for "view this one" without fighting the first --
                        // long-press is the idiomatic iOS choice for a cell's secondary action.
                        .onLongPressGesture { detail = DetailTarget(asset: asset) }
                    }
                }
                .padding(2)
            }
            .navigationTitle("\(selectedCount) of \(assets.count) selected")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Menu {
                        Button("Select all") { excludedAssetIDs.removeAll() }
                        Button("Deselect all") {
                            excludedAssetIDs = Set(assets.map(\.localIdentifier))
                        }
                    } label: {
                        Label("Selection", systemImage: "checklist")
                    }
                }
            }
            .sheet(item: $detail) { target in
                NavigationStack {
                    AssetDetailScreen(
                        asset: target.asset, loader: loader,
                        isExcluded: excludedBinding(for: target.asset))
                }
            }
        }

        private func toggle(_ asset: PHAsset) {
            let id = asset.localIdentifier
            if excludedAssetIDs.contains(id) {
                excludedAssetIDs.remove(id)
            } else {
                excludedAssetIDs.insert(id)
            }
        }

        private func excludedBinding(for asset: PHAsset) -> Binding<Bool> {
            Binding(
                get: { excludedAssetIDs.contains(asset.localIdentifier) },
                set: { isExcluded in
                    if isExcluded {
                        excludedAssetIDs.insert(asset.localIdentifier)
                    } else {
                        excludedAssetIDs.remove(asset.localIdentifier)
                    }
                })
        }
    }

    /// `PHAsset` has no `Identifiable` conformance of its own, and `.sheet(item:)` needs one.
    private struct DetailTarget: Identifiable {
        let asset: PHAsset
        var id: String { asset.localIdentifier }
    }

    /// One grid cell: thumbnail, dim-when-excluded overlay, and a duration badge for video.
    @available(iOS 17.0, *)
    private struct ThumbnailCell: View {
        let asset: PHAsset
        let loader: ThumbnailLoader
        let isExcluded: Bool

        @State private var image: UIImage?

        var body: some View {
            ZStack(alignment: .bottomTrailing) {
                Rectangle()
                    .fill(.gray.opacity(0.2))
                    .aspectRatio(1, contentMode: .fill)
                    .overlay {
                        if let image {
                            Image(uiImage: image)
                                .resizable()
                                .aspectRatio(contentMode: .fill)
                        } else {
                            ProgressView()
                        }
                    }
                    .clipped()
                    .opacity(isExcluded ? 0.35 : 1)

                if asset.mediaType == .video {
                    Text(Self.durationLabel(asset.duration))
                        .font(.caption2)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(.black.opacity(0.6), in: Capsule())
                        .foregroundStyle(.white)
                        .padding(4)
                }

                Image(systemName: isExcluded ? "circle" : "checkmark.circle.fill")
                    .foregroundStyle(isExcluded ? .white : .blue)
                    .background(isExcluded ? .clear : .white, in: Circle())
                    .padding(4)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing)
            }
            // Re-scrolling the grid re-runs `.task(id:)` on reappearance, but `ThumbnailLoader`'s
            // own cache is what stops that from re-requesting an image already fetched -- this is
            // the seam I18's acceptance criterion is about, not this view.
            .task(id: asset.localIdentifier) {
                image = await loader.thumbnail(for: asset)
            }
        }

        static func durationLabel(_ seconds: Double) -> String {
            let total = Int(seconds.rounded())
            return String(format: "%d:%02d", total / 60, total % 60)
        }
    }

    /// The full-screen single-asset page a long-press opens. One `PHImageManager` request at
    /// `ThumbnailLoader.detailTargetSize` -- bounded display size, never the original -- with the
    /// same deselect toggle available here as in the grid.
    @available(iOS 17.0, *)
    struct AssetDetailScreen: View {
        let asset: PHAsset
        let loader: ThumbnailLoader
        @Binding var isExcluded: Bool
        @Environment(\.dismiss) private var dismiss

        @State private var image: UIImage?

        var body: some View {
            VStack {
                ZStack {
                    Color.black
                    if let image {
                        Image(uiImage: image)
                            .resizable()
                            .aspectRatio(contentMode: .fit)
                    } else {
                        ProgressView().tint(.white)
                    }
                }
                Button(isExcluded ? "Include this item" : "Exclude this item") {
                    isExcluded.toggle()
                }
                .padding()
            }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .task(id: asset.localIdentifier) {
                image = await loader.thumbnail(for: asset, targetSize: ThumbnailLoader.detailTargetSize)
            }
        }
    }

#endif
