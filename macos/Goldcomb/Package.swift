// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Goldcomb",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "Goldcomb",
            path: "Sources/Goldcomb"
        ),
        .testTarget(
            name: "GoldcombTests",
            dependencies: ["Goldcomb"],
            path: "Tests/GoldcombTests"
        )
    ]
)
