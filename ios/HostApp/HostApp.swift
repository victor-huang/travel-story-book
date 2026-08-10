import SwiftUI

/// A test host, and nothing else.
///
/// `PhotoExportTests` needs an application bundle to exist at all: PhotoKit authorization goes
/// through TCC, and TCC attributes a request to a bundle identity. A bare SwiftPM test target
/// running under `xctest` has none, so `PHPhotoLibrary.requestAuthorization` fails with
/// "unable to construct an identity to kTCCServicePhotos" — not a missing grant, but nothing to
/// grant *to*. That is why an Xcode project exists beside the package (see the tracker, D10).
///
/// The real app is `Sources/StoryApp`. Nothing product-facing belongs here.
@main
struct HostApp: App {
    var body: some Scene {
        WindowGroup {
            Text("StoryBook test host")
                .padding()
        }
    }
}
