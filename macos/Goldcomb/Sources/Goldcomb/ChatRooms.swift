import Foundation
import UniformTypeIdentifiers

/// A file referenced by a chat message. The bytes live in the room's sidecar
/// (`.ai/chats/attachments/<chat-id>/…`); Python (chats.py) owns the format.
/// `path` is project-relative — resolve against the project dir to open it.
struct ChatAttachment: Equatable {
    let name: String   // original filename, for display
    let path: String   // project-relative path into the sidecar
    let mime: String
    let size: Int

    var isImage: Bool { mime.hasPrefix("image/") }

    /// How this attachment reads to an agent in the broker digest. Must stay
    /// word-for-word with chats.py `_attach_line`: images are flagged
    /// unviewable so a text-only model doesn't claim to have seen them; other
    /// files point at read_file.
    var digestLine: String {
        isImage
            ? "[image: \(name) — you cannot view images yet]"
            : "[attached: \(name) → \(path) — read_file it]"
    }

    static func decode(_ obj: [String: Any]) -> ChatAttachment? {
        guard let name = obj["name"] as? String,
              let path = obj["path"] as? String else { return nil }
        return ChatAttachment(
            name: name, path: path,
            mime: obj["mime"] as? String ?? "application/octet-stream",
            size: obj["size"] as? Int ?? 0)
    }
}

/// One message in an agent chat room (`.ai/chats/<id>.jsonl`).
struct ChatMessage: Identifiable, Equatable {
    let ts: Double
    let from: String
    let text: String
    let attachments: [ChatAttachment]

    init(ts: Double, from: String, text: String,
         attachments: [ChatAttachment] = []) {
        self.ts = ts; self.from = from; self.text = text
        self.attachments = attachments
    }

    var id: String { "\(ts)|\(from)|\(text.hashValue)" }
    /// The human owner posts as "user" (chats.py USER_HANDLE).
    var isUser: Bool { from == "user" }

    /// Does this message address `name`? Matches the bare name or an @handle
    /// on a word boundary, so "Art" doesn't match inside "start". Participants
    /// can carry a role suffix ("Quill (swift-worker-2)") — the given name is
    /// what anyone actually types, so match on that.
    func mentions(_ name: String) -> Bool {
        let given = name.split(separator: " ").first.map(String.init) ?? name
        guard !given.isEmpty else { return false }
        let escaped = NSRegularExpression.escapedPattern(for: given)
        return text.range(of: "(?<![\\w@])@?\(escaped)\\b",
                          options: [.regularExpression, .caseInsensitive]) != nil
    }
}

/// An agent-to-agent chat room, read from the project's shared `.ai/chats/`
/// directory. Python (goldcomb/chats.py) owns the format; this mirrors it:
/// line 1 header, then message lines, with participant joins as meta lines.
struct ChatRoom: Identifiable, Equatable {
    let id: String
    let title: String
    let kind: String            // "group" | "dm"
    let participants: [String]
    let messages: [ChatMessage]
    let url: URL

    var lastActivity: Double { messages.last?.ts ?? 0 }

    /// The project root this room lives under: `<proj>/.ai/chats/<id>.jsonl`
    /// → strip the file, then `chats`, then `.ai`.
    var projectDir: URL {
        url.deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    /// A room with no human participant — an agent↔agent DM. The user can see
    /// it (NEXA-69 shows these) but is not a member, so it is read-only to
    /// them: the composer is replaced with a note rather than injecting "user"
    /// into a conversation they aren't part of.
    var isAgentOnly: Bool { !participants.contains("user") }

    /// Agent messages since the human last spoke — the broker's loop brake:
    /// past a cap, delivery pauses and the room waits for the user.
    var unattendedCount: Int {
        if let idx = messages.lastIndex(where: { $0.isUser }) {
            return messages.count - idx - 1
        }
        return messages.count
    }

    /// Delivery pauses at this many agent messages with no human word —
    /// discussions get a few rounds, then the user gets the floor.
    var unattendedCap: Int { kind == "dm" ? 8 : 12 }
    var isPaused: Bool { unattendedCount >= unattendedCap }

    /// Should `name` be woken by `pending` — the messages they haven't seen?
    ///
    /// Waking every participant on every message is what makes a group chat
    /// quadratic: one post costs a full-context turn per other agent, and each
    /// of those may post again. So delivery follows what the message actually
    /// says. Naming someone addresses them; the human broadcasts (they're the
    /// one steering); and the message that opens a room broadcasts so a
    /// discussion can start. Anything else is a remark to the room — the user
    /// reads it in the app, nobody is woken, and the thread settles instead of
    /// ping-ponging on their bill.
    func addresses(_ name: String, in pending: [ChatMessage]) -> Bool {
        if pending.contains(where: { $0.mentions(name) }) { return true }
        // Someone else was named: this exchange isn't ours to answer.
        let namedAnyone = participants.contains { p in
            p != "user" && pending.contains { $0.mentions(p) }
        }
        if namedAnyone { return false }
        if pending.contains(where: { $0.isUser }) { return true }
        // Nothing has been read yet anywhere — this is the room's kickoff.
        return messages.count == pending.count
    }

    static func loadAll(projectDir: URL) -> [ChatRoom] {
        let dir = projectDir.appendingPathComponent(".ai/chats")
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: nil
        ) else { return [] }
        var out: [ChatRoom] = []
        for url in files where url.pathExtension == "jsonl" {
            if let room = load(url: url) { out.append(room) }
        }
        return out.sorted { $0.lastActivity > $1.lastActivity }
    }

    static func load(url: URL) -> ChatRoom? {
        guard let text = try? String(contentsOf: url, encoding: .utf8)
        else { return nil }
        var header: [String: Any]? = nil
        var participants: [String] = []
        var messages: [ChatMessage] = []
        for line in text.split(separator: "\n") {
            guard let data = line.data(using: .utf8),
                  let obj = (try? JSONSerialization.jsonObject(with: data))
                    as? [String: Any]
            else { continue }   // a torn concurrent append skips one line
            if header == nil, obj["kind"] != nil {
                header = obj
                participants = obj["participants"] as? [String] ?? []
                continue
            }
            if obj["meta"] as? String == "add" {
                for who in obj["who"] as? [String] ?? []
                where !participants.contains(who) {
                    participants.append(who)
                }
            } else if let from = obj["from"] as? String,
                      let body = obj["text"] as? String {
                // Optional per NEXA-74: absence is normal (pre-attachment
                // messages, other tools), so a missing key is not an error.
                let atts = (obj["attachments"] as? [[String: Any]] ?? [])
                    .compactMap(ChatAttachment.decode)
                messages.append(ChatMessage(
                    ts: obj["ts"] as? Double ?? 0, from: from, text: body,
                    attachments: atts))
            }
        }
        guard let h = header else { return nil }
        let id = h["id"] as? String ?? url.deletingPathExtension().lastPathComponent
        return ChatRoom(id: id,
                        title: h["title"] as? String ?? id,
                        kind: h["kind"] as? String ?? "group",
                        participants: participants,
                        messages: messages,
                        url: url)
    }

    // MARK: writes (the user's side of the conversation)

    private static func appendLine(_ obj: [String: Any], to url: URL) {
        guard let data = try? JSONSerialization.data(withJSONObject: obj),
              var line = String(data: data, encoding: .utf8)
        else { return }
        line += "\n"
        if let handle = try? FileHandle(forWritingTo: url) {
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: Data(line.utf8))
        }
    }

    /// Post as the human participant. Same one-line append the Python side
    /// does, so concurrent writers interleave lines, not bytes. Attachments
    /// are copied into the room sidecar BEFORE the line is written (the
    /// copy-before-append invariant chats.py holds), so a delivered reference
    /// never dangles; a file that fails to copy is simply dropped from the
    /// message rather than left pointing at nothing.
    func postAsUser(_ text: String, attachments: [URL] = []) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let refs = attachments.compactMap {
            Self.storeAttachment($0, chatID: id, projectDir: projectDir)
        }
        guard !trimmed.isEmpty || !refs.isEmpty else { return }
        var obj: [String: Any] = [
            "ts": Date().timeIntervalSince1970, "from": "user", "text": trimmed,
        ]
        if !refs.isEmpty { obj["attachments"] = refs }
        Self.appendLine(obj, to: url)
    }

    /// Cap mirrors chats.py MAX_ATTACH_BYTES — a reference store, not a dump.
    static let maxAttachBytes = 25 * 1024 * 1024

    /// Copy one file into the room's sidecar and return its reference record
    /// (`{name, path, mime, size}`, path project-relative), or nil if it can't
    /// be read / is over the cap / the copy fails. The macOS composer's half
    /// of NEXA-74's "one storage rule, both writers" — byte-for-byte the same
    /// layout chats.py writes, so either side can read the other's rooms.
    static func storeAttachment(_ src: URL, chatID: String,
                                projectDir: URL) -> [String: Any]? {
        let fm = FileManager.default
        guard let attrs = try? fm.attributesOfItem(atPath: src.path),
              let size = attrs[.size] as? Int, size <= maxAttachBytes
        else { return nil }
        let destDir = projectDir
            .appendingPathComponent(".ai/chats/attachments/\(chatID)")
        try? fm.createDirectory(at: destDir, withIntermediateDirectories: true)
        let name = src.lastPathComponent
        let safe = name
            .replacingOccurrences(of: "[^A-Za-z0-9._-]+", with: "-",
                                  options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "-."))
        let stamp = Int(Date().timeIntervalSince1970 * 1000)
        var dest = destDir.appendingPathComponent("\(stamp)-\(safe.isEmpty ? "file" : safe)")
        var n = 0
        while fm.fileExists(atPath: dest.path) {
            n += 1
            dest = destDir.appendingPathComponent("\(stamp)-\(n)-\(safe)")
        }
        do { try fm.copyItem(at: src, to: dest) } catch { return nil }
        let rel = ".ai/chats/attachments/\(chatID)/\(dest.lastPathComponent)"
        let mime = UTType(filenameExtension: src.pathExtension)?
            .preferredMIMEType ?? "application/octet-stream"
        return ["name": name, "path": rel, "mime": mime, "size": size]
    }

    /// Resolve an attachment's project-relative path to an absolute URL for
    /// opening / thumbnailing.
    func attachmentURL(_ attachment: ChatAttachment) -> URL {
        projectDir.appendingPathComponent(attachment.path)
    }

    /// User-created group chat: same file format chats.py writes.
    @discardableResult
    static func create(title: String, participants: [String],
                       projectDir: URL) -> String? {
        let dir = projectDir.appendingPathComponent(".ai/chats")
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true)
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyyMMdd-HHmmss"
        let slug = title.lowercased()
            .replacingOccurrences(of: "[^a-z0-9]+", with: "-",
                                  options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        let id = "\(fmt.string(from: Date()))-\(String(slug.prefix(24)))"
        var names = participants
        if !names.contains("user") { names.append("user") }
        let url = dir.appendingPathComponent("\(id).jsonl")
        guard !FileManager.default.fileExists(atPath: url.path),
              FileManager.default.createFile(atPath: url.path, contents: nil)
        else { return nil }
        appendLine(["id": id, "kind": "group", "title": title,
                    "participants": names,
                    "created": Date().timeIntervalSince1970], to: url)
        return id
    }
}

/// Per-chat "how many messages the user has seen" — backs unread badges.
/// UserDefaults is plenty: it's a small [chatID: count] map.
enum ChatReadState {
    private static let key = "chatReadCounts"

    static func counts() -> [String: Int] {
        UserDefaults.standard.dictionary(forKey: key) as? [String: Int] ?? [:]
    }

    static func markRead(_ room: ChatRoom) {
        var all = counts()
        all[room.id] = room.messages.count
        UserDefaults.standard.set(all, forKey: key)
    }

    static func unread(_ room: ChatRoom) -> Int {
        max(0, room.messages.count - seenCount(room))
    }

    /// How many messages the user has already seen — clamped to the room so a
    /// shrunk/rewritten file can't index past its end (jump-to-first-unread).
    static func seenCount(_ room: ChatRoom) -> Int {
        min(counts()[room.id] ?? 0, room.messages.count)
    }
}
