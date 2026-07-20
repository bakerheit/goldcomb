import XCTest
@testable import Goldcomb

/// Chat attachments (NEXA-74 format contract). Python (chats.py) writes the
/// JSONL; the app reads it back, so the parser must accept the exact shape and
/// tolerate its absence. The digest wording must match chats.py `_attach_line`
/// word for word — the agent on the other side reads it literally.
final class ChatAttachmentTests: XCTestCase {

    private func writeRoom(_ lines: [String]) throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("room.jsonl")
        try lines.joined(separator: "\n").write(
            to: url, atomically: true, encoding: .utf8)
        return url
    }

    private let header =
        #"{"id":"room","kind":"group","title":"R","participants":["Ada","Quill","user"]}"#

    func testParsesAttachmentsField() throws {
        let msg = #"""
        {"ts":1,"from":"Ada","text":"see this","attachments":[{"name":"diff.patch","path":".ai/chats/attachments/room/1-diff.patch","mime":"text/x-patch","size":42}]}
        """#
        let room = ChatRoom.load(url: try writeRoom([header, msg]))
        let atts = try XCTUnwrap(room?.messages.first?.attachments)
        XCTAssertEqual(atts.count, 1)
        XCTAssertEqual(atts[0].name, "diff.patch")
        XCTAssertEqual(atts[0].size, 42)
        XCTAssertFalse(atts[0].isImage)
    }

    func testMessageWithoutAttachmentsHasEmptyArray() throws {
        let msg = #"{"ts":1,"from":"Ada","text":"plain"}"#
        let room = ChatRoom.load(url: try writeRoom([header, msg]))
        XCTAssertEqual(room?.messages.first?.attachments, [])
    }

    func testImageAttachmentIsFlaggedUnviewable() throws {
        let msg = #"""
        {"ts":1,"from":"Ada","text":"","attachments":[{"name":"ui.png","path":".ai/chats/attachments/room/1-ui.png","mime":"image/png","size":10}]}
        """#
        let room = ChatRoom.load(url: try writeRoom([header, msg]))
        let att = try XCTUnwrap(room?.messages.first?.attachments.first)
        XCTAssertTrue(att.isImage)
        // Word-for-word with chats.py `_attach_line`.
        XCTAssertEqual(att.digestLine, "[image: ui.png — you cannot view images yet]")
    }

    func testNonImageDigestPointsAtReadFile() {
        let att = ChatAttachment(
            name: "log.txt", path: ".ai/chats/attachments/room/1-log.txt",
            mime: "text/plain", size: 5)
        XCTAssertEqual(
            att.digestLine,
            "[attached: log.txt → .ai/chats/attachments/room/1-log.txt — read_file it]")
    }

    func testMalformedAttachmentEntryIsSkippedNotFatal() throws {
        // Missing `path` → that entry drops; a good sibling still parses.
        let msg = #"""
        {"ts":1,"from":"Ada","text":"x","attachments":[{"name":"broken"},{"name":"ok.txt","path":"p","mime":"text/plain","size":1}]}
        """#
        let room = ChatRoom.load(url: try writeRoom([header, msg]))
        let atts = try XCTUnwrap(room?.messages.first?.attachments)
        XCTAssertEqual(atts.map(\.name), ["ok.txt"])
    }

    // MARK: - the app-composer writer (NEXA-78 storage side)

    /// A room rooted at <proj>/.ai/chats/<id>.jsonl so postAsUser can derive
    /// its projectDir and sidecar.
    private func projectRoom() throws -> (ChatRoom, URL) {
        let proj = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        let dir = proj.appendingPathComponent(".ai/chats")
        try FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("room.jsonl")
        try (header + "\n").write(to: url, atomically: true, encoding: .utf8)
        return (try XCTUnwrap(ChatRoom.load(url: url)), proj)
    }

    func testComposerPostCopiesFileIntoSidecarAndRoundTrips() throws {
        let (room, proj) = try projectRoom()
        let srcDir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: srcDir, withIntermediateDirectories: true)
        let src = srcDir.appendingPathComponent("report.txt")
        try "findings".write(to: src, atomically: true, encoding: .utf8)

        room.postAsUser("here it is", attachments: [src])

        // Re-read from disk: the line and the copied bytes must both be there.
        let reloaded = try XCTUnwrap(ChatRoom.load(url: room.url))
        let msg = try XCTUnwrap(reloaded.messages.last)
        XCTAssertEqual(msg.text, "here it is")
        let att = try XCTUnwrap(msg.attachments.first)
        XCTAssertEqual(att.name, "report.txt")
        XCTAssertTrue(att.path.hasPrefix(".ai/chats/attachments/\(room.id)/"))
        // The sidecar copy exists and the original is untouched.
        let copied = proj.appendingPathComponent(att.path)
        XCTAssertEqual(try String(contentsOf: copied, encoding: .utf8), "findings")
        XCTAssertTrue(FileManager.default.fileExists(atPath: src.path))
    }

    func testComposerPostWithOnlyAttachmentIsValid() throws {
        let (room, _) = try projectRoom()
        let src = FileManager.default.temporaryDirectory
            .appendingPathComponent("\(UUID().uuidString).png")
        try Data([0x89]).write(to: src)
        room.postAsUser("", attachments: [src])
        let reloaded = try XCTUnwrap(ChatRoom.load(url: room.url))
        XCTAssertEqual(reloaded.messages.last?.attachments.count, 1)
    }

    func testEmptyPostWritesNothing() throws {
        let (room, _) = try projectRoom()
        let before = ChatRoom.load(url: room.url)?.messages.count ?? -1
        room.postAsUser("   ", attachments: [])
        XCTAssertEqual(ChatRoom.load(url: room.url)?.messages.count, before)
    }

    func testAgentOnlyRoomIsReadOnly() {
        let group = ChatRoom(id: "g", title: "G", kind: "group",
                             participants: ["Ada", "user"], messages: [],
                             url: URL(fileURLWithPath: "/tmp/g.jsonl"))
        let dm = ChatRoom(id: "d", title: "D", kind: "dm",
                          participants: ["Ada", "Quill"], messages: [],
                          url: URL(fileURLWithPath: "/tmp/d.jsonl"))
        XCTAssertFalse(group.isAgentOnly)
        XCTAssertTrue(dm.isAgentOnly)
    }
}
