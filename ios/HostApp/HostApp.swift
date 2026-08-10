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
/// **A way to run M0.** It presents `ExportScreen` (I17) so the export path can be exercised by
/// tapping rather than by a test. I23 builds the real shell and this goes away.
@main
struct HostApp: App {
    var body: some Scene {
        WindowGroup {
            ExportScreen()
        }
    }
}
