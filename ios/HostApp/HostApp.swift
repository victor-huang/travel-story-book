import StoryApp
import SwiftUI

/// Two jobs, both temporary.
///
/// **A test host.** `PhotoExportTests` needs an application bundle to exist at all: PhotoKit
/// authorization goes through TCC, and TCC attributes a request to a bundle identity. A bare
/// SwiftPM test target has none, so `PHPhotoLibrary.requestAuthorization` fails with "unable to
/// construct an identity to kTCCServicePhotos" — not a missing grant, but nothing to grant *to*.
/// See the tracker, D10.
///
/// **A way to run the whole loop.** Two tabs: `ExportScreen` (I17) picks media and writes a local
/// folder, `LoopScreen` (I26) sends that folder to the service, builds, and renders the report.
/// Both are already thin — this just gives a human a way to reach either without Xcode. I23
/// builds the real shell and this goes away.
@main
struct HostApp: App {
    var body: some Scene {
        WindowGroup {
            TabView {
                ExportScreen()
                    .tabItem { Label("Export", systemImage: "square.and.arrow.up") }
                LoopScreen()
                    .tabItem { Label("Send", systemImage: "arrow.triangle.2.circlepath") }
            }
        }
    }
}
