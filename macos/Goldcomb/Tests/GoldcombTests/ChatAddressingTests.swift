import XCTest
@testable import Goldcomb

/// Chat delivery is the broker's most expensive decision: waking an agent
/// re-sends its entire working context to the model, so a room that wakes
/// every participant on every message costs a full-context turn per agent per
/// message — and each reply does it again. These tests pin who actually gets
/// woken.
final class ChatAddressingTests: XCTestCase {

    private func room(_ texts: [(String, String)],
                      participants: [String] = ["Quill", "Ada", "user"],
                      kind: String = "group") -> ChatRoom {
        ChatRoom(
            id: "r", title: "Room", kind: kind, participants: participants,
            messages: texts.enumerated().map { i, m in
                ChatMessage(ts: Double(i), from: m.0, text: m.1)
            },
            url: URL(fileURLWithPath: "/tmp/r.jsonl"))
    }

    // MARK: mention matching

    func testMatchesBareNameAndAtHandle() {
        let bare = ChatMessage(ts: 0, from: "Ada", text: "Quill, can you check X?")
        let handle = ChatMessage(ts: 0, from: "Ada", text: "cc @Quill on this")
        XCTAssertTrue(bare.mentions("Quill"))
        XCTAssertTrue(handle.mentions("Quill"))
    }

    func testMatchIsCaseInsensitiveAndIgnoresRoleSuffix() {
        let m = ChatMessage(ts: 0, from: "Ada", text: "quill: take a look")
        XCTAssertTrue(m.mentions("Quill (swift-worker-2)"))
    }

    func testDoesNotMatchNameInsideAnotherWord() {
        let m = ChatMessage(ts: 0, from: "Ada", text: "let's start the build")
        XCTAssertFalse(m.mentions("Art"))
    }

    // MARK: who gets woken

    func testKickoffWakesEveryone() {
        let r = room([("Ada", "let's figure out the caching bug")])
        XCTAssertTrue(r.addresses("Quill", in: r.messages))
    }

    func testUserBroadcastsWhenNobodyIsNamed() {
        let r = room([("Ada", "opening"), ("user", "status update please")])
        XCTAssertTrue(r.addresses("Quill", in: [r.messages[1]]))
    }

    func testNamingOneAgentDoesNotWakeTheOthers() {
        let r = room([("user", "kickoff"), ("Ada", "Quill, can you check X?")])
        let pending = [r.messages[1]]
        XCTAssertTrue(r.addresses("Quill", in: pending))
        XCTAssertFalse(r.addresses("Ada", in: pending))
    }

    /// A user @-tag sets expectation, not exclusivity: the tagged agent is
    /// expected to reply, but the others are still woken so they can chime in.
    func testUserTagExpectsOneButLetsOthersChimeIn() {
        let r = room([("Ada", "opening"), ("user", "@Quill ship it")])
        let pending = [r.messages[1]]
        // Quill is woken AND specifically expected.
        XCTAssertTrue(r.addresses("Quill", in: pending))
        XCTAssertTrue(r.expects("Quill", in: pending))
        // Ada is woken too (may chime in) but is NOT the expected responder.
        XCTAssertTrue(r.addresses("Ada", in: pending))
        XCTAssertFalse(r.expects("Ada", in: pending))
    }

    func testTaggedAgentsListsWhoWasAsked() {
        let r = room([("user", "@Quill and @Ada please weigh in")])
        XCTAssertEqual(Set(r.taggedAgents(in: r.messages)), ["Quill", "Ada"])
    }

    /// An untagged user post still broadcasts (the user's floor) — everyone
    /// can chime in, and nobody is the singled-out expected responder.
    func testUntaggedUserPostBroadcastsWithNoOneExpected() {
        let r = room([("Ada", "opening"), ("user", "what do we all think?")])
        let pending = [r.messages[1]]
        XCTAssertTrue(r.addresses("Ada", in: pending))
        XCTAssertTrue(r.addresses("Quill", in: pending))
        XCTAssertFalse(r.expects("Ada", in: pending))
        XCTAssertFalse(r.expects("Quill", in: pending))
    }

    /// The loop brake: an agent talking to the room without addressing anyone
    /// wakes nobody, so a discussion settles instead of ping-ponging.
    func testUnaddressedRemarkWakesNobody() {
        let r = room([("user", "kickoff"), ("Ada", "opening"),
                      ("Ada", "that seems reasonable to me")])
        XCTAssertFalse(r.addresses("Quill", in: [r.messages[2]]))
    }

    func testDirectMessageStillReachesItsRecipient() {
        let r = room([("Ada", "here's the trace"), ("Ada", "any thoughts?")],
                     participants: ["Ada", "Quill"], kind: "dm")
        XCTAssertTrue(r.addresses("Quill", in: r.messages))
    }
}
