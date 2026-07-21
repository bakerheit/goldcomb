import XCTest
@testable import Goldcomb

/// Block-level Markdown parsing for chat rendering. Agents emit headings,
/// lists, quotes, and fenced code constantly; SwiftUI's AttributedString only
/// handles inline syntax, so these were showing raw. The parser is pure, so
/// the block segmentation is pinned here independent of the SwiftUI view.
final class MarkdownParseTests: XCTestCase {

    func testHeadingsByLevel() {
        XCTAssertEqual(Markdown.parse("## Suggested roadmap"),
                       [.heading(level: 2, text: "Suggested roadmap")])
        XCTAssertEqual(Markdown.parse("### Near term"),
                       [.heading(level: 3, text: "Near term")])
    }

    func testHashWithoutSpaceIsNotAHeading() {
        // "#tag" is text, not a heading.
        XCTAssertEqual(Markdown.parse("#tag line"), [.paragraph("#tag line")])
    }

    func testClosingHashesAreStripped() {
        XCTAssertEqual(Markdown.parse("## Title ##"),
                       [.heading(level: 2, text: "Title")])
    }

    func testBulletListGroups() {
        let md = "- What changed?\n- Can I review it?\n- What is it costing me?"
        XCTAssertEqual(Markdown.parse(md), [.bulletList([
            "What changed?", "Can I review it?", "What is it costing me?"])])
    }

    func testOrderedListPreservesSourceNumbers() {
        // The screenshot's list starts at 11 — the numbers must not be recounted.
        let md = "11. Multi-agent orchestration\n12. Artifacts\n13. Plugin/MCP support"
        XCTAssertEqual(Markdown.parse(md), [.orderedList([
            MarkdownOrderedItem(number: "11", text: "Multi-agent orchestration"),
            MarkdownOrderedItem(number: "12", text: "Artifacts"),
            MarkdownOrderedItem(number: "13", text: "Plugin/MCP support"),
        ])])
    }

    func testBlockquoteGathersConsecutiveLines() {
        let md = "> Changes panel + Git integration\n> permission profiles"
        XCTAssertEqual(Markdown.parse(md),
                       [.quote("Changes panel + Git integration\npermission profiles")])
    }

    func testFencedCodeIsVerbatim() {
        let md = "```\nlet x = 1\n// a comment\n```"
        XCTAssertEqual(Markdown.parse(md), [.code("let x = 1\n// a comment")])
    }

    func testHorizontalRule() {
        XCTAssertEqual(Markdown.parse("---"), [.rule])
        XCTAssertEqual(Markdown.parse("***"), [.rule])
    }

    func testParagraphsSplitOnBlankLines() {
        let md = "First paragraph line\nstill first.\n\nSecond paragraph."
        XCTAssertEqual(Markdown.parse(md), [
            .paragraph("First paragraph line\nstill first."),
            .paragraph("Second paragraph."),
        ])
    }

    func testInlineMarkersLeftIntactForInlineRenderer() {
        // Bold/italic stay in the text — inline rendering handles them, not the
        // block parser.
        XCTAssertEqual(Markdown.parse("This is **bold** text"),
                       [.paragraph("This is **bold** text")])
    }

    func testMixedDocumentFromTheScreenshot() {
        let md = """
        ## Suggested roadmap

        ### Near term

        1. Token/cost dashboard
        2. Diff/change review panel

        > **Changes panel + Git integration**

        - What changed?
        - Can I review it?
        """
        let blocks = Markdown.parse(md)
        XCTAssertEqual(blocks, [
            .heading(level: 2, text: "Suggested roadmap"),
            .heading(level: 3, text: "Near term"),
            .orderedList([
                MarkdownOrderedItem(number: "1", text: "Token/cost dashboard"),
                MarkdownOrderedItem(number: "2", text: "Diff/change review panel"),
            ]),
            .quote("**Changes panel + Git integration**"),
            .bulletList(["What changed?", "Can I review it?"]),
        ])
    }
}
