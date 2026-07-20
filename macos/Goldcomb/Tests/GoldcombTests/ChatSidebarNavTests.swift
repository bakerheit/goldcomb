import XCTest
@testable import Goldcomb

/// SessionStore's chat-sidebar navigation state (NEXA-71 room routing, NEXA-84
/// ticket jump). Pure in-memory against `SessionStore(forTesting:)`.
final class ChatSidebarNavTests: XCTestCase {

    private func makeStore() -> SessionStore { SessionStore(forTesting: true) }

    private func room(id: String, in project: Project,
                      kind: String = "group",
                      participants: [String] = ["Ada", "user"]) -> ChatRoom {
        let url = project.directory
            .appendingPathComponent(".ai/chats/\(id).jsonl")
        return ChatRoom(id: id, title: id, kind: kind,
                        participants: participants, messages: [], url: url)
    }

    func testRoomLookupAcrossProjects() {
        let store = makeStore()
        let a = store.createProject(name: "A", directory: URL(fileURLWithPath: "/tmp/a"))
        let b = store.createProject(name: "B", directory: URL(fileURLWithPath: "/tmp/b"))
        store.chats = [a.id: [room(id: "r1", in: a)],
                       b.id: [room(id: "r2", in: b)]]
        XCTAssertEqual(store.room(withID: "r2")?.id, "r2")
        XCTAssertNil(store.room(withID: "nope"))
    }

    func testProjectIDForRoomMatchesByDirectory() {
        let store = makeStore()
        let p = store.createProject(name: "P", directory: URL(fileURLWithPath: "/tmp/proj"))
        let r = room(id: "r", in: p)
        XCTAssertEqual(store.projectID(forRoom: r), p.id)
    }

    func testFocusTicketSelectsProjectAndFlagsTicket() {
        let store = makeStore()
        let p = store.createProject(name: "P", directory: URL(fileURLWithPath: "/tmp/p"))
        store.selection = .chat("some-room")
        store.focusTicket("NEXA-84", in: p.id)
        XCTAssertEqual(store.selection, .project(p.id))
        XCTAssertEqual(store.pendingTicketFocus, "NEXA-84")
    }

    func testMarkChatReadBumpsTheReDiffTick() {
        let store = makeStore()
        let p = store.createProject(name: "P", directory: URL(fileURLWithPath: "/tmp/p"))
        let r = room(id: "r", in: p)
        let before = store.chatReadTick
        store.markChatRead(r)
        XCTAssertGreaterThan(store.chatReadTick, before)
    }

    func testAgentOnlyRoomStillResolves() {
        // NEXA-69: agent-only DMs appear in the sidebar, so they must resolve
        // for a `.chat` selection like any other room.
        let store = makeStore()
        let p = store.createProject(name: "P", directory: URL(fileURLWithPath: "/tmp/p"))
        let dm = room(id: "dm1", in: p, kind: "dm", participants: ["Ada", "Quill"])
        store.chats = [p.id: [dm]]
        XCTAssertTrue(store.room(withID: "dm1")?.isAgentOnly ?? false)
    }
}
