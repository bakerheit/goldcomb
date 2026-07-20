import SwiftUI

/// Project tab showing every agent chat room (group discussions and DMs).
/// The user reads everything and posts as a first-class participant; agent
/// deliveries are handled by the store's broker, not this view.
struct ChatsTabView: View {
    @EnvironmentObject var store: SessionStore
    let project: Project

    @State private var selectedID: String? = nil
    @State private var showNew = false

    private var rooms: [ChatRoom] { store.chats[project.id] ?? [] }
    private var selected: ChatRoom? {
        rooms.first { $0.id == selectedID } ?? rooms.first
    }

    var body: some View {
        HStack(spacing: 0) {
            roomList
                .frame(width: 262)
            Divider()
            if let room = selected {
                // One room view, shared with the sidebar `.chat` selection.
                ChatRoomView(room: room)
                    .id(room.id)
            } else {
                ContentUnavailableView(
                    "No chats yet",
                    systemImage: "bubble.left.and.bubble.right",
                    description: Text("Agents start discussions with the chat "
                                      + "tool — ask your planner to convene "
                                      + "sprint planning — or start one here.")
                )
                .frame(maxWidth: .infinity)
            }
        }
        .sheet(isPresented: $showNew) {
            NewChatSheet(project: project) { id in
                selectedID = id
                store.refreshChatsNow()
            }
        }
    }

    // MARK: room list

    private var roomList: some View {
        VStack(spacing: 0) {
            HStack {
                Text("CHATS")
                    .font(.system(size: 10, weight: .semibold))
                    .kerning(0.8)
                    .foregroundStyle(.tertiary)
                Spacer()
                Button {
                    showNew = true
                } label: {
                    Image(systemName: "plus.bubble")
                }
                .buttonStyle(.plain)
                .help("Start a group chat with this project's agents")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            List(selection: $selectedID) {
                ForEach(rooms) { room in
                    roomRow(room)
                        .tag(room.id)
                        .listRowSeparator(.hidden)
                }
            }
            .listStyle(.plain)
        }
    }

    private func roomRow(_ room: ChatRoom) -> some View {
        let _ = store.chatReadTick  // re-diff the badge when a room is read
        let unread = ChatReadState.unread(room)
        return HStack(spacing: 8) {
            Image(systemName: room.kind == "dm"
                  ? "person.line.dotted.person" : "person.3")
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(room.title)
                    .font(.callout)
                    .lineLimit(1)
                if let last = room.messages.last {
                    Text("\(last.isUser ? "You" : shortName(last.from)): \(last.text)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                } else {
                    Text(room.participants.map(shortName).joined(separator: ", "))
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 4)
            if room.isPaused {
                Image(systemName: "hand.raised")
                    .font(.caption2)
                    .foregroundStyle(Comb.honey)
                    .help("Discussion paused — the agents are waiting for you")
            }
            if unread > 0 {
                Text("\(unread)")
                    .font(.system(size: 10, weight: .bold))
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Comb.gold.opacity(0.9), in: Capsule())
                    .foregroundStyle(.white)
            }
        }
        .padding(.vertical, 2)
    }

    /// "Quill Ashwood (swift-worker-2)" → "Quill Ashwood" for tight rows.
    private func shortName(_ name: String) -> String {
        name == "user" ? "You"
            : String(name.split(separator: "(").first ?? Substring(name))
                .trimmingCharacters(in: .whitespaces)
    }
}

/// User-started group chat: pick teammates, name the topic, optionally open
/// with a first message (which the broker then delivers to everyone).
struct NewChatSheet: View {
    @EnvironmentObject var store: SessionStore
    @Environment(\.dismiss) private var dismiss
    let project: Project
    var onCreate: (String) -> Void

    @State private var title = ""
    @State private var included: Set<String> = []
    @State private var firstMessage = ""

    private var teammates: [String] {
        store.sessionsFor(project).map(\.name)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("New group chat").font(.headline)
            TextField("Topic (e.g. Sprint planning)", text: $title)
                .textFieldStyle(.roundedBorder)
            if teammates.isEmpty {
                Text("No agents in this project yet.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text("PARTICIPANTS")
                        .font(.system(size: 10, weight: .semibold))
                        .kerning(0.8)
                        .foregroundStyle(.tertiary)
                    ForEach(teammates, id: \.self) { name in
                        Toggle(name, isOn: Binding(
                            get: { included.contains(name) },
                            set: { on in
                                if on { included.insert(name) }
                                else { included.remove(name) }
                            }))
                    }
                }
            }
            TextField("Opening message (optional)", text: $firstMessage,
                      axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(2...4)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Start chat") {
                    guard let id = ChatRoom.create(
                        title: title, participants: Array(included),
                        projectDir: project.directory)
                    else { return }
                    if !firstMessage.trimmingCharacters(
                        in: .whitespacesAndNewlines).isEmpty,
                       let room = ChatRoom.load(
                        url: project.directory.appendingPathComponent(
                            ".ai/chats/\(id).jsonl")) {
                        room.postAsUser(firstMessage)
                    }
                    onCreate(id)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines)
                            .isEmpty || included.isEmpty)
            }
        }
        .padding(20)
        .frame(width: 380)
    }
}
