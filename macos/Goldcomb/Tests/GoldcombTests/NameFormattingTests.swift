import XCTest
@testable import Goldcomb

/// Shared display-name formatting (NEXA-116). The logic was duplicated in
/// ChatRoomView and ChatsTabView; one home now, pinned here.
final class NameFormattingTests: XCTestCase {

    func testHumanHandleBecomesYou() {
        XCTAssertEqual(NameFormatting.shortName("user"), "You")
    }

    func testRoleSuffixIsDropped() {
        XCTAssertEqual(NameFormatting.shortName("Quill Ashwood (swift-worker-2)"),
                       "Quill Ashwood")
    }

    func testPlainNameIsUntouched() {
        XCTAssertEqual(NameFormatting.shortName("Beatrix Winslow"), "Beatrix Winslow")
    }

    func testWhitespaceIsTrimmed() {
        XCTAssertEqual(NameFormatting.shortName("  Lark Fen  "), "Lark Fen")
    }
}
