import XCTest
@testable import Goldcomb

/// Shared send-button policy (NEXA-110). The two composers had drifted on how
/// emptiness is measured; one definition now, pinned here. The send/stop
/// button itself is a view.
final class ComposerSendRulesTests: XCTestCase {

    // MARK: hasText

    func testEmptyDraftHasNoText() {
        XCTAssertFalse(ComposerSendRules.hasText(""))
    }

    func testWhitespaceOnlyDraftHasNoText() {
        XCTAssertFalse(ComposerSendRules.hasText("  \n\t  "))
    }

    func testNonWhitespaceDraftHasText() {
        XCTAssertTrue(ComposerSendRules.hasText("  hi  "))
    }

    // MARK: canSend

    func testCanSendWithTextOnly() {
        XCTAssertTrue(ComposerSendRules.canSend(draft: "hello", staged: 0))
    }

    func testCanSendWithAttachmentsOnly() {
        XCTAssertTrue(ComposerSendRules.canSend(draft: "", staged: 2))
    }

    func testCannotSendEmptyAndUnstaged() {
        XCTAssertFalse(ComposerSendRules.canSend(draft: "  ", staged: 0))
    }

    func testModeGateBlocksSend() {
        XCTAssertFalse(ComposerSendRules.canSend(draft: "hello", staged: 1,
                                                 modeGate: false))
    }
}
