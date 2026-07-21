import XCTest
@testable import Goldcomb

/// Git working-tree status (NEXA-97): the ProjectView header decodes the
/// serve protocol's `git_status` event into a GitStatus and groups the
/// changed files by porcelain bucket. This pins the decode + grouping the
/// header UI depends on.
final class GitStatusTests: XCTestCase {

    private let sampleEvent: [String: Any] = [
        "event": "git_status",
        "branch": "main",
        "ahead": 2,
        "behind": 1,
        "files": [
            ["path": "Sources/App.swift", "status": "staged"],
            ["path": "README.md", "status": "unstaged"],
            ["path": "Sources/Util.swift", "status": "unstaged"],
            ["path": "notes.txt", "status": "untracked"],
        ],
    ]

    func testDecodesBranchAndCounts() {
        let git = GitStatus.decode(event: sampleEvent)
        XCTAssertNotNil(git)
        XCTAssertEqual(git?.branch, "main")
        XCTAssertEqual(git?.ahead, 2)
        XCTAssertEqual(git?.behind, 1)
    }

    func testDirtyCount() {
        let git = GitStatus.decode(event: sampleEvent)
        XCTAssertEqual(git?.dirtyCount, 4)
        XCTAssertEqual(git?.isClean, false)
    }

    func testGroupedFileLists() {
        let git = GitStatus.decode(event: sampleEvent)!
        XCTAssertEqual(git.staged.map(\.path), ["Sources/App.swift"])
        XCTAssertEqual(git.unstaged.map(\.path),
                       ["README.md", "Sources/Util.swift"])
        XCTAssertEqual(git.untracked.map(\.path), ["notes.txt"])
    }

    func testCleanTree() {
        let git = GitStatus.decode(event: [
            "event": "git_status", "branch": "dev", "files": [[String: Any]](),
        ])
        XCTAssertEqual(git?.branch, "dev")
        XCTAssertEqual(git?.dirtyCount, 0)
        XCTAssertEqual(git?.isClean, true)
        // Missing ahead/behind default to 0.
        XCTAssertEqual(git?.ahead, 0)
        XCTAssertEqual(git?.behind, 0)
    }

    func testMissingBranchFailsToDecode() {
        // No branch field → nil (the header then renders nothing git-related).
        XCTAssertNil(GitStatus.decode(event: ["event": "git_status"]))
    }

    func testMalformedFilesAreSkipped() {
        // Files missing a path or with an unknown status are dropped, not fatal.
        let git = GitStatus.decode(event: [
            "branch": "main",
            "files": [
                ["path": "ok.swift", "status": "staged"],
                ["status": "unstaged"],                 // no path
                ["path": "bad.swift", "status": "bogus"], // unknown status
            ],
        ])
        XCTAssertEqual(git?.dirtyCount, 1)
        XCTAssertEqual(git?.staged.map(\.path), ["ok.swift"])
    }
}
