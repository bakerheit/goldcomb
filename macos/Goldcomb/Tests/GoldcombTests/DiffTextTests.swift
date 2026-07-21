import XCTest
@testable import Goldcomb

/// Inline diff viewer (NEXA-99): the parser that classifies unified-diff
/// lines for tinting, and the decode of the serve protocol's `git_diff`
/// event that DiffView renders in the changed-file sheet.
final class DiffTextTests: XCTestCase {

    private let sampleDiff = """
    diff --git a/Sources/App.swift b/Sources/App.swift
    index 1111111..2222222 100644
    --- a/Sources/App.swift
    +++ b/Sources/App.swift
    @@ -1,3 +1,4 @@
     context line
    -removed line
    +added line
    +another add
     tail context
    """

    private func kinds(_ source: String) -> [DiffLine.Kind] {
        DiffParse.parse(source).map(\.kind)
    }

    func testClassifiesAllLineKinds() {
        XCTAssertEqual(kinds(sampleDiff), [
            .fileHeader,  // diff --git
            .fileHeader,  // index
            .fileHeader,  // ---
            .fileHeader,  // +++
            .hunk,        // @@
            .context,
            .del,
            .add,
            .add,
            .context,
        ])
    }

    func testFileHeaderBeatsAddDel() {
        // The longer prefix wins: "---"/"+++" are headers, not del/add.
        XCTAssertEqual(DiffParse.classify("--- a/f.swift"), .fileHeader)
        XCTAssertEqual(DiffParse.classify("+++ b/f.swift"), .fileHeader)
        XCTAssertEqual(DiffParse.classify("-x"), .del)
        XCTAssertEqual(DiffParse.classify("+x"), .add)
    }

    func testNoNewlineMarkerIsContext() {
        XCTAssertEqual(DiffParse.classify("\\ No newline at end of file"), .context)
    }

    func testEmptyDiffParsesToNoLines() {
        XCTAssertEqual(DiffParse.parse(""), [DiffLine(kind: .context, text: "", offset: 0)])
    }

    func testUntrackedNoticePassesThroughAsContext() {
        let notice = "notes.txt: new file, no diff (untracked — use git add to stage)"
        XCTAssertEqual(kinds(notice), [.context])
        XCTAssertEqual(DiffParse.parse(notice).first?.text, notice)
    }

    func testParseKeepsLineTextVerbatim() {
        let lines = DiffParse.parse(sampleDiff)
        XCTAssertEqual(lines.map(\.text), sampleDiff.components(separatedBy: "\n"))
        // Offsets are stable ids in source order.
        XCTAssertEqual(lines.map(\.offset), Array(0..<lines.count))
    }

    // MARK: git_diff event decode

    func testDecodesGitDiffEvent() {
        let reply = GitDiff.decode(event: [
            "event": "git_diff",
            "path": "Sources/App.swift",
            "staged": true,
            "diff": sampleDiff,
            "truncated": false,
        ])
        XCTAssertEqual(reply?.path, "Sources/App.swift")
        XCTAssertEqual(reply?.staged, true)
        XCTAssertEqual(reply?.diff, sampleDiff)
        XCTAssertEqual(reply?.truncated, false)
    }

    func testDecodeDefaultsStagedAndTruncated() {
        // Untracked-notice reply: same shape, flags may be absent/false.
        let reply = GitDiff.decode(event: [
            "event": "git_diff",
            "path": "notes.txt",
            "diff": "notes.txt: new file, no diff (untracked — use git add to stage)",
        ])
        XCTAssertEqual(reply?.staged, false)
        XCTAssertEqual(reply?.truncated, false)
    }

    func testDecodeTruncatedFlag() {
        let reply = GitDiff.decode(event: [
            "event": "git_diff", "path": "big.bin", "staged": false,
            "diff": "…", "truncated": true,
        ])
        XCTAssertEqual(reply?.truncated, true)
    }

    func testMalformedEventFailsToDecode() {
        // Missing path or diff → nil; the sheet shows an inline error.
        XCTAssertNil(GitDiff.decode(event: ["event": "git_diff", "diff": "x"]))
        XCTAssertNil(GitDiff.decode(event: ["event": "git_diff", "path": "f.swift"]))
        XCTAssertNil(GitDiff.decode(event: ["event": "git_diff"]))
    }
}
