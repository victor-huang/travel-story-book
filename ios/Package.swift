// swift-tools-version: 6.0

// Four targets per dev_plan/ios_implementation_tracker.md. StoryKit and PhotoExport build and
// test without an app scheme — that is the argument for a Swift Package over an Xcode project.
// macOS is listed so `swift build && swift test` runs on CI without a simulator.

import PackageDescription

let package = Package(
    name: "StoryBook",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    // Products exist so the Xcode test host (StoryBookHost.xcodeproj) can link these targets.
    // PhotoKit authorization needs an app bundle identity, which a bare SwiftPM test target has
    // no way to obtain — see the tracker, D10.
    products: [
        .library(name: "StoryKit", targets: ["StoryKit"]),
        .library(name: "PhotoExport", targets: ["PhotoExport"]),
        .library(name: "StoryService", targets: ["StoryService"]),
        .library(name: "StoryApp", targets: ["StoryApp"]),
    ],
    targets: [
        // Pure logic. No UIKit, no SwiftUI, no PhotoKit.
        .target(name: "StoryKit"),
        // PhotoKit + ImageIO + AVFoundation.
        .target(name: "PhotoExport", dependencies: ["StoryKit"]),
        // URLSession client.
        .target(name: "StoryService", dependencies: ["StoryKit"]),
        // SwiftUI. Stays thin enough that no logic hides in a view.
        .target(name: "StoryApp", dependencies: ["StoryKit", "PhotoExport", "StoryService"]),
        .testTarget(
            name: "StoryKitTests",
            dependencies: ["StoryKit"],
            resources: [.copy("Fixtures")]
        ),
        .testTarget(name: "PhotoExportTests", dependencies: ["PhotoExport"]),
        .testTarget(name: "StoryServiceTests", dependencies: ["StoryService"]),
        // Added by D13. Wave 2 onward is mostly StoryApp, and there was nowhere to put its tests.
        .testTarget(
            name: "StoryAppTests",
            dependencies: ["StoryApp"],
            resources: [.copy("Fixtures")]
        ),
    ]
)
