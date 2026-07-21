import XCTest
@testable import Goldcomb

/// The group-chat `@`-mention autocomplete parsing: when the popup triggers,
/// what it filters to, and how a pick rewrites the draft. Pure logic, pinned
/// here so the reactive popup behavior is verified without SwiftUI.
final class MentionAutocompleteTests: XCTestCase {

    private let agents = ["Quill Ashwood (swift-worker-2)", "Quinn Vale", "Ada Byte"]

    // MARK: active-mention detection

    func testDetectsMentionBeingTyped() {
        let m = MentionAutocomplete.active(in: "hey @qu")
        XCTAssertEqual(m?.query, "qu")
    }

    func testBareAtTriggersWithEmptyQuery() {
        let m = MentionAutocomplete.active(in: "ping @")
        XCTAssertEqual(m?.query, "")   // popup should show everyone
    }

    func testSpaceClosesTheMention() {
        // Once a space follows the tag, the user's moved on — no popup.
        XCTAssertNil(MentionAutocomplete.active(in: "@Quill ship it"))
    }

    func testEmailDoesNotTrigger() {
        XCTAssertNil(MentionAutocomplete.active(in: "mail me at foo@bar"))
    }

    func testOnlyTheLastMentionIsActive() {
        let m = MentionAutocomplete.active(in: "@Quill and @ad")
        XCTAssertEqual(m?.query, "ad")
    }

    func testNoAtMeansNoMention() {
        XCTAssertNil(MentionAutocomplete.active(in: "just a message"))
    }

    // MARK: filtering (reactive search)

    func testEmptyQueryReturnsEveryone() {
        XCTAssertEqual(MentionAutocomplete.suggestions(agents, matching: "").count, 3)
    }

    func testFiltersByGivenNameCaseInsensitively() {
        // "qu" matches both Quill and Quinn, not Ada.
        let s = MentionAutocomplete.suggestions(agents, matching: "QU")
        XCTAssertEqual(s, ["Quill Ashwood (swift-worker-2)", "Quinn Vale"])
    }

    func testSubstringSearchNotJustPrefix() {
        // "da" is inside "Ada".
        XCTAssertEqual(MentionAutocomplete.suggestions(agents, matching: "da"),
                       ["Ada Byte"])
    }

    func testPrefixMatchesRankFirst() {
        // "al": Val contains it (index 1), Alex has it as a prefix — Alex first
        // even though it comes second in the list.
        let people = ["Val", "Alex"]
        XCTAssertEqual(MentionAutocomplete.suggestions(people, matching: "al"),
                       ["Alex", "Val"])
    }

    // MARK: applying a pick

    func testPickReplacesThePartialWithGivenName() {
        let out = MentionAutocomplete.applying("Quill Ashwood (swift-worker-2)",
                                               to: "hey @qu")
        XCTAssertEqual(out, "hey @Quill ")
    }

    func testPickWithNoActiveMentionAppends() {
        let out = MentionAutocomplete.applying("Ada Byte", to: "look here")
        XCTAssertEqual(out, "look here @Ada ")
    }

    func testInsertedTagMatchesTheBrokerMatcher() {
        // The whole point: what autocomplete inserts must be what the broker
        // recognizes as a tag.
        let out = MentionAutocomplete.applying("Quill Ashwood (swift-worker-2)",
                                               to: "@qu")
        let msg = ChatMessage(ts: 0, from: "user", text: out)
        XCTAssertTrue(msg.mentions("Quill Ashwood (swift-worker-2)"))
    }
}
