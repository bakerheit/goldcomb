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
            Text(room.participants.map { $0 == "user" ? "you" : NameFormatting.shortName($0) }
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

    /// Layer ticket-id links and the @user highlight onto a message's
    /// Markdown-parsed inline runs, in the room's palette.
    private func decorate(_ attr: AttributedString) -> AttributedString {
        ChatLinkRouter.decorate(attr, linkColor: Comb.gold, userColor: Comb.honey)
    }

    @ViewBuilder
    private func bubble(_ message: ChatMessage, grouped: Bool) -> some View {
        if message.isUser {
            HStack {
                Spacer(minLength: 60)
                VStack(alignment: .trailing, spacing: 2) {
                    if !message.text.isEmpty {
                        MarkdownMessage(text: message.text, decorate: decorate)
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
                            Text(NameFormatting.shortName(message.from))
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Comb.tint(for: message.from))
                            Text(timestamp(message.ts))
                                .font(.system(size: 9))
                                .foregroundStyle(.tertiary)
                        }
                    }
                    if !message.text.isEmpty {
                        MarkdownMessage(text: message.text, decorate: decorate)
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
        // Shared composer core (NEXA-110): tray + field + paperclip + send.
        // Room slots: the mention autocomplete popup / tag hint rides the
        // accessory strip, the mention menu sits leading of the field.
        SharedComposerView(
            draft: $draft,
            staged: $pending,
            placeholder: "Message \(room.kind == "dm" ? "this DM" : room.title)…",
            lineLimit: 1...5,
            canSend: canSend,
            onSubmit: {
                // Return picks the top suggestion while the popup is open,
                // otherwise sends.
                if let first = mentionSuggestions.first {
                    applyMention(first)
                } else {
                    send()
                }
            },
            onSend: send,
            onAttach: { showImporter = true },
            leading: { mentionMenu },
            trailing: { EmptyView() }
        ) {
            // While the user is typing a mention, the autocomplete popup takes
            // the slot; otherwise, if the draft already tags anyone, the hint.
            if !mentionSuggestions.isEmpty {
                mentionPopup
            } else if !taggedInDraft.isEmpty {
                tagHint
            }
        }
        .padding(.horizontal, 4)
        // Room composer's drop surface (NEXA-112): dropped files stage into
        // `pending`. The tray overrides preview routing with the room's own
        // QuickLook binding above.
        .inAttachmentDropSurface(urls: $pending)
    }

    /// The room's agents (the human isn't a taggable participant).
    private var agentParticipants: [String] {
        room.participants.filter { $0 != "user" }
    }

    // MARK: @-mention autocomplete

    /// Agents matching the `@…` the user is currently typing (empty when they
    /// aren't mid-mention). Drives the popup; reactive on every keystroke.
    private var mentionSuggestions: [String] {
        guard let (_, query) = MentionAutocomplete.active(in: draft) else { return [] }
        return MentionAutocomplete.suggestions(agentParticipants, matching: query)
    }

    private var mentionPopup: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(mentionSuggestions.enumerated()), id: \.element) { i, name in
                Button { applyMention(name) } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "at")
                            .foregroundStyle(Comb.gold).frame(width: 16)
                        Text(NameFormatting.shortName(name)).font(.callout)
                        Spacer(minLength: 4)
                        if i == 0 {  // Return picks the top one
                            Image(systemName: "return")
                                .font(.caption2).foregroundStyle(.tertiary)
                        }
                    }
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .contentShape(Rectangle())
                    .background(i == 0 ? Comb.gold.opacity(0.12) : .clear,
                                in: RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(3)
        .frame(maxWidth: 260, alignment: .leading)
        .background(.quaternary.opacity(0.6),
                    in: RoundedRectangle(cornerRadius: 8))
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Tag an agent so it's the expected responder. Replaces the `@partial`
    /// being typed (or appends) with "@Given " — the broker matches on the
    /// given name, and others can still chime in.
    private func applyMention(_ name: String) {
        draft = MentionAutocomplete.applying(name, to: draft)
    }

    /// Discovery fallback: the same list from a button, for when the user
    /// hasn't started typing `@`.
    private var mentionMenu: some View {
        Menu {
            ForEach(agentParticipants, id: \.self) { name in
                Button("@\(NameFormatting.shortName(name))") { applyMention(name) }
            }
        } label: {
            Image(systemName: "at").font(.body)
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .disabled(agentParticipants.isEmpty)
        .help("Tag an agent — they're expected to reply; others may chime in")
    }

    /// Agents currently @-tagged in the draft — live feedback that the tag is
    /// recognized (matched the same way the broker matches on delivery).
    private var taggedInDraft: [String] {
        let probe = ChatMessage(ts: 0, from: "user", text: draft)
        return agentParticipants.filter { probe.mentions($0) }
    }

    private var tagHint: some View {
        HStack(spacing: 4) {
            Image(systemName: "at.circle.fill")
                .font(.caption2).foregroundStyle(Comb.gold)
            Text("Expecting a reply from "
                 + taggedInDraft.map(NameFormatting.shortName).joined(separator: ", ")
                 + " — others may still chime in")
                .font(.caption2).foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer()
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
        // Shared rule (NEXA-110): text or staged attachments; rooms have no
        // extra gate (no live-process requirement like user↔agent chat).
        ComposerSendRules.canSend(draft: draft, staged: pending.count)
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
}
