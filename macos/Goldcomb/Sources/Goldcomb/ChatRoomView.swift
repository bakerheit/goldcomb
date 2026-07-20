import QuickLook
import SwiftUI

/// One chat room's transcript + composer, reused by the Chats tab and by a
/// `.chat` sidebar selection (NEXA-66/71). Everything that renders or writes a
/// single room lives here so the two entry points can never drift.
///
/// Attachments (NEXA-74/78): images render as inline thumbnails (click for
/// QuickLook), other files as chips (click to preview, right-click to reveal).
/// The composer stages files, then copies them into the room sidecar on send.
/// Agent-only rooms (no human participant, NEXA-69) are read-only.
struct ChatRoomView: View {
    @EnvironmentObject var store: SessionStore
    let room: ChatRoom

    @State private var draft = ""
    /// Files staged in the composer, not yet posted.
    @State private var pending: [URL] = []
    @State private var showImporter = false
    /// QuickLook target; non-nil presents the preview panel.
    @State private var preview: URL?

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            transcript
            Divider()
            if room.isAgentOnly {
                readOnlyNote
            } else {
                composer
            }
        }
        .frame(maxWidth: .infinity)
        .quickLookPreview($preview)
        // A tapped ticket link routes to that ticket's Sprint view (NEXA-84);
        // anything else falls through to the system.
        .environment(\.openURL, OpenURLAction { url in
            if let ticket = ChatLinkRouter.ticket(from: url) {
                store.focusTicket(ticket, in: store.projectID(forRoom: room))
                return .handled
            }
            return .systemAction
        })
        .fileImporter(isPresented: $showImporter,
                      allowedContentTypes: [.item],
                      allowsMultipleSelection: true) { result in
            if case .success(let urls) = result {
                pending.append(contentsOf: urls)
            }
        }
    }

    // MARK: header

    private var header: some View {
        HStack(spacing: 6) {
            Text(room.title).font(.headline)
            Text(room.participants.map { $0 == "user" ? "you" : shortName($0) }
                    .joined(separator: " · "))
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            if room.isAgentOnly {
                Text("agent-only")
                    .font(.system(size: 9, weight: .bold))
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(.quaternary, in: Capsule())
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    // MARK: transcript

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 3) {
                    ForEach(room.messages) { message in
                        if startsNewDay(message) {
                            daySeparator(message.ts)
                        }
                        bubble(message, grouped: isGroupedWithPrevious(message))
                            .id(message.id)
                    }
                    if room.isPaused {
                        pausedBanner
                    }
                }
                .padding(14)
            }
            .onAppear {
                jumpToFirstUnread(proxy)
                markRead()
            }
            .onChange(of: room.messages.count) { _, _ in
                withAnimation {
                    proxy.scrollTo(room.messages.last?.id, anchor: .bottom)
                }
                markRead()
            }
            .onChange(of: room.id) { _, _ in
                jumpToFirstUnread(proxy)
                markRead()
            }
        }
    }

    /// Land the reader on the first message they haven't seen (NEXA-66's
    /// folded-in lurker win) rather than always at the bottom; if they're
    /// caught up, the bottom is the right place.
    private func jumpToFirstUnread(_ proxy: ScrollViewProxy) {
        let seen = ChatReadState.seenCount(room)
        if seen < room.messages.count, seen > 0 {
            proxy.scrollTo(room.messages[seen].id, anchor: .top)
        } else {
            proxy.scrollTo(room.messages.last?.id, anchor: .bottom)
        }
    }

    // MARK: bubble

    @ViewBuilder
    private func bubble(_ message: ChatMessage, grouped: Bool) -> some View {
        if message.isUser {
            HStack {
                Spacer(minLength: 60)
                VStack(alignment: .trailing, spacing: 2) {
                    if !message.text.isEmpty {
                        Text(ChatLinkRouter.attributed(
                                message.text, linkColor: Comb.gold,
                                userColor: Comb.honey))
                            .textSelection(.enabled)
                            .padding(.horizontal, 11).padding(.vertical, 7)
                            .background(Comb.gold.opacity(0.22),
                                        in: RoundedRectangle(cornerRadius: 10))
                    }
                    attachments(message, alignment: .trailing)
                    if !grouped {
                        Text(timestamp(message.ts))
                            .font(.system(size: 9))
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            .padding(.top, grouped ? 0 : 7)
        } else {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    if !grouped {
                        HStack(spacing: 6) {
                            Text(shortName(message.from))
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Comb.tint(for: message.from))
                            Text(timestamp(message.ts))
                                .font(.system(size: 9))
                                .foregroundStyle(.tertiary)
                        }
                    }
                    if !message.text.isEmpty {
                        Text(ChatLinkRouter.attributed(
                                message.text, linkColor: Comb.gold,
                                userColor: Comb.honey))
                            .textSelection(.enabled)
                            .padding(.horizontal, 11).padding(.vertical, 7)
                            .background(.quaternary.opacity(0.5),
                                        in: RoundedRectangle(cornerRadius: 10))
                    }
                    attachments(message, alignment: .leading)
                }
                Spacer(minLength: 60)
            }
            .padding(.top, grouped ? 0 : 7)
        }
    }

    // MARK: attachment rendering

    @ViewBuilder
    private func attachments(_ message: ChatMessage,
                             alignment: HorizontalAlignment) -> some View {
        if !message.attachments.isEmpty {
            VStack(alignment: alignment, spacing: 4) {
                ForEach(message.attachments, id: \.path) { att in
                    attachmentView(att)
                }
            }
        }
    }

    @ViewBuilder
    private func attachmentView(_ att: ChatAttachment) -> some View {
        let url = room.attachmentURL(att)
        if att.isImage, let image = NSImage(contentsOf: url) {
            Image(nsImage: image)
                .resizable()
                .scaledToFit()
                .frame(maxWidth: 240, maxHeight: 240)
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .onTapGesture { preview = url }
                .help("Click to preview \(att.name)")
                .contextMenu { revealButton(url) }
        } else {
            Button {
                preview = url
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: att.isImage ? "photo" : "doc")
                        .foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 0) {
                        Text(att.name).font(.caption).lineLimit(1)
                        Text(byteLabel(att.size))
                            .font(.system(size: 9)).foregroundStyle(.tertiary)
                    }
                }
                .padding(.horizontal, 9).padding(.vertical, 6)
                .background(.quaternary.opacity(0.5),
                            in: RoundedRectangle(cornerRadius: 8))
            }
            .buttonStyle(.plain)
            .contextMenu { revealButton(url) }
        }
    }

    private func revealButton(_ url: URL) -> some View {
        Button("Reveal in Finder") {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }

    // MARK: composer

    private var composer: some View {
        VStack(spacing: 6) {
            if !pending.isEmpty {
                pendingTray
            }
            HStack(spacing: 8) {
                Button {
                    showImporter = true
                } label: {
                    Image(systemName: "paperclip").font(.body)
                }
                .buttonStyle(.plain)
                .help("Attach files")
                TextField("Message \(room.kind == "dm" ? "this DM" : room.title)…",
                          text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...5)
                    .onSubmit(send)
                Button(action: send) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.title3)
                        .foregroundStyle(canSend ? AnyShapeStyle(Comb.gold)
                                                 : AnyShapeStyle(.tertiary))
                }
                .buttonStyle(.plain)
                .disabled(!canSend)
                .help("Post to this chat — participants are notified")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    /// Staged-but-unsent attachments, each removable before posting.
    private var pendingTray: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(pending, id: \.self) { url in
                    HStack(spacing: 4) {
                        Image(systemName: "doc").font(.caption2)
                        Text(url.lastPathComponent).font(.caption2).lineLimit(1)
                        Button {
                            pending.removeAll { $0 == url }
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.caption2).foregroundStyle(.tertiary)
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(.horizontal, 7).padding(.vertical, 3)
                    .background(.quaternary.opacity(0.5), in: Capsule())
                }
            }
        }
    }

    private var readOnlyNote: some View {
        HStack(spacing: 6) {
            Image(systemName: "eye")
            Text("You're viewing an agent-to-agent DM — read-only.")
                .font(.caption)
            Spacer()
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 14).padding(.vertical, 12)
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || !pending.isEmpty
    }

    private func send() {
        guard canSend else { return }
        room.postAsUser(draft.trimmingCharacters(in: .whitespacesAndNewlines),
                        attachments: pending)
        draft = ""
        pending = []
        store.refreshChatsNow()
    }

    private func markRead() {
        store.markChatRead(room)
    }

    // MARK: grouping / day separators

    private func isGroupedWithPrevious(_ message: ChatMessage) -> Bool {
        guard let idx = room.messages.firstIndex(where: { $0.id == message.id }),
              idx > 0
        else { return false }
        return room.messages[idx - 1].from == message.from
            && !startsNewDay(message)
    }

    private func startsNewDay(_ message: ChatMessage) -> Bool {
        guard let idx = room.messages.firstIndex(where: { $0.id == message.id }),
              idx > 0
        else { return true }
        let cal = Calendar.current
        let prev = Date(timeIntervalSince1970: room.messages[idx - 1].ts)
        return !cal.isDate(prev, inSameDayAs: Date(timeIntervalSince1970: message.ts))
    }

    private func daySeparator(_ ts: Double) -> some View {
        let date = Date(timeIntervalSince1970: ts)
        let label: String
        if Calendar.current.isDateInToday(date) {
            label = "Today"
        } else if Calendar.current.isDateInYesterday(date) {
            label = "Yesterday"
        } else {
            let fmt = DateFormatter()
            fmt.dateFormat = "EEEE, MMM d"
            label = fmt.string(from: date)
        }
        return HStack(spacing: 8) {
            VStack { Divider() }
            Text(label)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.tertiary)
            VStack { Divider() }
        }
        .padding(.vertical, 6)
    }

    private var pausedBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "hand.raised")
            Text("The agents have paused after \(room.unattendedCount) "
                 + "messages — post below to give direction and continue "
                 + "the discussion.")
                .font(.caption)
        }
        .foregroundStyle(Comb.honey)
        .padding(10)
        .frame(maxWidth: .infinity)
        .background(Comb.honey.opacity(0.08),
                    in: RoundedRectangle(cornerRadius: 8))
    }

    // MARK: text helpers

    private func timestamp(_ ts: Double) -> String {
        let date = Date(timeIntervalSince1970: ts)
        let fmt = DateFormatter()
        fmt.dateFormat = Calendar.current.isDateInToday(date)
            ? "HH:mm" : "MMM d, HH:mm"
        return fmt.string(from: date)
    }

    private func byteLabel(_ size: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file)
    }

    private func shortName(_ name: String) -> String {
        name == "user" ? "You"
            : String(name.split(separator: "(").first ?? Substring(name))
                .trimmingCharacters(in: .whitespaces)
    }
}
