import SwiftUI
import UniformTypeIdentifiers

struct ChatView: View {
    @EnvironmentObject var store: SessionStore
    @ObservedObject var session: AgentSession
    @State private var draft = ""
    @State private var showHistory = false
    @State private var historyThreads: [ThreadSummary] = []
    @State private var attachments: [URL] = []
    @State private var showFilePicker = false
    @State private var pickedProvider = ""
    @State private var pickedModel = ""
    @State private var showQuestionSheet = false

    /// A tapped ticket link routes to that ticket's Sprint view (NEXA-84);
    /// anything else falls through to the system. Set at the top level so the
    /// whole transcript (and TranscriptRow stays store-free) inherits it.
    private var openTicketLink: OpenURLAction {
        OpenURLAction { url in
            if let ticket = ChatLinkRouter.ticket(from: url) {
                store.focusTicket(ticket, in: store.projectID(forDirectory: session.directory))
                return .handled
            }
            return .systemAction
        }
    }

    /// Layer ticket-id links and the @user highlight onto a message's
    /// Markdown-parsed inline runs, in the app palette.
    private func decorate(_ attr: AttributedString) -> AttributedString {
        ChatLinkRouter.decorate(attr, linkColor: Comb.gold, userColor: Comb.honey)
    }

    var body: some View {
        VStack(spacing: 0) {
            transcript
            // Inline recovery for a pending turn-state interruption: the
            // alert/sheet above can be lost (e.g. the confirm alert's
            // `set: { if !$0 → nil }` binding when switching windows); these
            // banners are the non-modal path back to the same decision UI.
            if session.pendingConfirm != nil || session.pendingQuestions != nil {
                Divider()
                pendingInterruptionBanner
            }
            if session.crashOffered {
                Divider()
                relaunchBanner
            }
            Divider()
            statusBar
            inputBar
        }
        .navigationTitle(session.name)
        .navigationSubtitle("\(session.provider) · \(session.model)")
        .toolbar {
            ToolbarItemGroup {
                Button {
                    reloadHistory()
                    showHistory = true
                } label: {
                    Label("History", systemImage: "clock.arrow.circlepath")
                }
                .help("This agent's past conversations")
                .popover(isPresented: $showHistory, arrowEdge: .bottom) {
                    // Reload on appear, not only in the button action: setting
                    // the list @State in the same cycle that flips showHistory
                    // races the popover's first render, so it came up empty and
                    // only filled on a second open. onAppear fires each time the
                    // popover is shown, so it's always fresh (and picks up newly
                    // saved conversations too).
                    historyPopover
                        .onAppear(perform: reloadHistory)
                }
                Button {
                    session.interrupt()
                } label: {
                    Label("Interrupt", systemImage: "stop.circle")
                }
                .disabled(!session.isRunning)
                .help("Abort the current turn (like Ctrl-C)")
                Menu {
                    if let tid = session.threadId {
                        Text("Chat id: \(tid)")
                        Button("Copy chat id") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(tid, forType: .string)
                        }
                    } else {
                        Text("No chat id yet — send a message first")
                    }
                    Divider()
                    Button("Copy agent name") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(session.name, forType: .string)
                    }
                    Button("Copy project path") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(session.directory.path,
                                                       forType: .string)
                    }
                } label: {
                    Label("More", systemImage: "ellipsis.circle")
                }
                .help("Chat details")
            }
        }
        .sheet(isPresented: Binding(
            get: { session.pendingQuestions != nil || showQuestionSheet },
            set: { if !$0 { showQuestionSheet = false } }  // dismissal also via the sheet's buttons
        )) {
            if let questions = session.pendingQuestions {
                QuestionSheet(questions: questions) { answers in
                    session.sendAnswers(answers)
                }
                .interactiveDismissDisabled()
            }
        }
        .alert(
            "Run this tool call?",
            isPresented: Binding(
                get: { session.pendingConfirm != nil },
                set: { if !$0 { session.pendingConfirm = nil } }
            )
        ) {
            Button("Run") { session.respondToConfirm("yes") }
            Button("Always for this tool") { session.respondToConfirm("always") }
            Button("Skip", role: .cancel) { session.respondToConfirm("no") }
            Button("Abort turn", role: .destructive) { session.respondToConfirm("abort") }
        } message: {
            Text(session.pendingConfirm ?? "")
        }
        .environment(\.openURL, openTicketLink)
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                if session.transcript.isEmpty {
                    // New session: nothing to show yet — same empty-state
                    // pattern as ChatsTabView's "No chats yet".
                    ContentUnavailableView(
                        "No messages yet",
                        systemImage: "bubble.left",
                        description: Text("Send a message below to start the "
                                          + "conversation with \(session.name).")
                    )
                    .frame(maxWidth: .infinity)
                    .containerRelativeFrame(.vertical) { h, _ in max(h - 28, 0) }
                } else {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(Array(session.transcript.enumerated()),
                                id: \.element.id) { idx, item in
                            // Day separators + row timestamps (NEXA-118), the
                            // room transcript's scheme ported onto
                            // TranscriptItem.ts. Mostly invisible in a live
                            // chat (same-day rows get a plain HH:mm); it earns
                            // its keep on resumed threads spanning days.
                            if idx == 0
                                || TranscriptTime.startsNewDay(
                                    item.ts, after: session.transcript[idx - 1].ts) {
                                TranscriptDaySeparator(date: item.ts)
                            }
                            TranscriptRow(item: item, decorate: decorate,
                                          ts: item.ts, showTimestamp: true)
                                .id(item.id)
                        }
                    }
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            // Opening a chat should land on the newest message, not the top.
            // The onChange handlers below only fire once this view is already
            // observing, so a transcript that's already populated when the view
            // appears (switching agents, or after a resume hydrates history)
            // would otherwise sit at the first message. Jump to the end on
            // appear — async so the LazyVStack has laid out its rows first.
            .onAppear { scrollToEnd(proxy, animated: false) }
            .onChange(of: session.transcript.count) {
                scrollToEnd(proxy, animated: true)
            }
            .onChange(of: session.transcript.last?.text) {
                scrollToEnd(proxy, animated: true)
            }
        }
    }

    private func scrollToEnd(_ proxy: ScrollViewProxy, animated: Bool) {
        guard let last = session.transcript.last else { return }
        let jump = { proxy.scrollTo(last.id, anchor: .bottom) }
        // On first appear the rows aren't laid out yet, so scrolling in the
        // same tick no-ops; defer a tick. (Live updates are already laid out.)
        if animated {
            withAnimation { jump() }
        } else {
            DispatchQueue.main.async(execute: jump)
        }
    }

    /// Non-modal recovery for a pending confirm/question: shows the waiting
    /// state inline and re-opens the same modal/sheet used for the decision.
    @ViewBuilder
    private var pendingInterruptionBanner: some View {
        HStack(spacing: 10) {
            Image(systemName: "hand.raised.fill")
                .foregroundStyle(.orange)
            if let summary = session.pendingConfirm {
                Text("Waiting for approval: \(summary)")
                    .font(.callout)
                    .lineLimit(1)
            } else if let questions = session.pendingQuestions {
                Text(questions.count == 1
                     ? "Waiting for an answer to 1 question"
                     : "Waiting for answers to \(questions.count) questions")
                    .font(.callout)
                    .lineLimit(1)
            }
            Spacer()
            if session.pendingConfirm != nil {
                Button("Review…") { /* the confirm alert re-presents automatically */ }
                    .controlSize(.small)
            } else if session.pendingQuestions != nil {
                Button("Answer…") { showQuestionSheet = true }
                    .controlSize(.small)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Color.orange.opacity(0.12))
    }

    /// Crash recovery: shown after an unexpected process exit (surfaced as an
    /// .error transcript line). Relaunches the agent and resumes the last
    /// thread when one exists (reuses the NDJSON `resume` command).
    private var relaunchBanner: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.red)
            Text(session.threadId != nil
                 ? "Agent stopped unexpectedly — relaunch and resume this conversation?"
                 : "Agent stopped unexpectedly — relaunch it?")
                .font(.callout)
                .lineLimit(2)
            Spacer()
            Button(session.threadId != nil ? "Relaunch & Resume" : "Relaunch") {
                session.relaunchAgent()
            }
            .controlSize(.small)
            .buttonStyle(.borderedProminent)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Color.red.opacity(0.12))
    }

    private var statusBar: some View {
        HStack(spacing: 10) {
            if let status = session.status {
                ProgressView().controlSize(.small)
                Text(status).font(.callout).foregroundStyle(.secondary)
            } else if !session.isAlive {
                Image(systemName: "bolt.slash")
                Text(session.crashOffered ? "agent process crashed" : "agent process exited")
                    .font(.callout)
                    .foregroundStyle(session.crashOffered ? Color.red : .secondary)
            } else {
                Text("idle").font(.callout).foregroundStyle(.tertiary)
            }
            Spacer()
            Text("⬆\(session.sessionIn.formattedTokens) ⬇\(session.sessionOut.formattedTokens)")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
        .background(.bar)
    }

    /// (Re)load this agent's saved conversations for the history popover. Only
    /// this agent's own threads: legacy aliases count as the same identity,
    /// sub-agent threads don't (NEXA-31 matching).
    private func reloadHistory() {
        historyThreads = ThreadSummary
            .loadAll(projectDir: session.directory)
            .filter { AgentIdentity.matches(session.name, headerAgent: $0.agent)
                      && !AgentIdentity.isSubagent($0.agent) }
    }

    /// This agent's saved conversations: click one to resume it in place.
    private var historyPopover: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("\(session.name)'s conversations")
                .font(.headline)
                .padding(12)
            Divider()
            if historyThreads.isEmpty {
                Text("No saved conversations yet.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(16)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(historyThreads) { thread in
                            Button {
                                session.resumeThread(id: thread.id)
                                showHistory = false
                            } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(thread.title)
                                        .font(.callout)
                                        .lineLimit(1)
                                    Text("\(thread.updated.prefix(16).replacingOccurrences(of: "T", with: " "))  ·  \(thread.messageCount) msgs")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .padding(.horizontal, 12).padding(.vertical, 6)
                            .disabled(session.isRunning || !session.isAlive)
                            .help("Resume this conversation")
                        }
                    }
                    .padding(.vertical, 6)
                }
                .frame(maxHeight: 320)
            }
        }
        .frame(width: 340)
    }

    private var inputBar: some View {
        // Shared composer core (NEXA-110): tray + field + paperclip + send/stop.
        // Agent-chat slots: the slash palette rides the accessory strip; the
        // utility row (commands, model chip, sudo, new-conversation) is this
        // stack's own and stays below.
        VStack(spacing: 6) {
            SharedComposerView(
                draft: $draft,
                staged: $attachments,
                placeholder: "Message the agent…",
                trayTint: Comb.gold,
                lineLimit: 1...6,
                showsStop: session.isRunning,
                buttonSize: 28,
                canSend: canSend,
                onSubmit: sendDraft,
                onSend: sendDraft,
                onStop: session.interrupt,
                onAttach: { showFilePicker = true },
                leading: { EmptyView() },
                trailing: { EmptyView() }
            ) {
                if !slashSuggestions.isEmpty {
                    slashPalette
                }
            }
            utilityRow
        }
        .padding(.horizontal, 10)
        .padding(.bottom, 10)
        .fileImporter(
            isPresented: $showFilePicker,
            allowedContentTypes: [.item],
            allowsMultipleSelection: true
        ) { result in
            if case .success(let urls) = result {
                attachments += urls.filter { !attachments.contains($0) }
            }
        }
        .inAttachmentDropSurface(urls: $attachments)
    }

    /// Commands matching what the user has typed after "/", shown above the
    /// composer. Clicking one runs it.
    private var slashSuggestions: [SlashCommand] {
        SlashCommands.suggestions(for: draft)
    }

    private var slashPalette: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(slashSuggestions) { cmd in
                Button {
                    if cmd.isEnabled(session) { cmd.run(session) }
                    draft = ""
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: cmd.icon)
                            .foregroundStyle(Comb.gold)
                            .frame(width: 18)
                        Text("/\(cmd.name)").font(.callout.weight(.medium))
                        Text(cmd.subtitle)
                            .font(.caption).foregroundStyle(.secondary)
                            .lineLimit(1)
                        Spacer(minLength: 4)
                    }
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!cmd.isEnabled(session))
                .opacity(cmd.isEnabled(session) ? 1 : 0.4)
            }
        }
        .background(.quaternary.opacity(0.5),
                    in: RoundedRectangle(cornerRadius: 8))
    }

    /// The button strip under the field: attach, model switcher, sudo, and
    /// new-conversation.
    private var utilityRow: some View {
        HStack(spacing: 12) {
            Button {
                showFilePicker = true
            } label: {
                Image(systemName: "paperclip")
                    .frame(width: 32, height: 30)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Attach files (or drop them here)")

            commandMenu

            modelChip

            Spacer()

            Button {
                session.setSudo(!session.sudo)
            } label: {
                Image(systemName: session.sudo
                      ? "checkmark.shield.fill" : "shield")
                    .foregroundStyle(session.sudo ? Comb.amber : .secondary)
                    .frame(width: 32, height: 30)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(!session.isAlive)
            .help(session.sudo ? "sudo on — click to turn off"
                                : "sudo off — click to turn on")

            Button {
                session.clearConversation()
            } label: {
                Image(systemName: "square.and.pencil")
                    .frame(width: 32, height: 30)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(!session.isAlive || session.isRunning)
            .help("New conversation (current is saved)")
        }
        .font(.title3)
        .foregroundStyle(.secondary)
    }

    /// Slash commands, discoverable without typing "/". Same actions the
    /// composer palette runs.
    private var commandMenu: some View {
        Menu {
            ForEach(SlashCommands.all) { cmd in
                Button {
                    if cmd.isEnabled(session) { cmd.run(session) }
                } label: {
                    Label("/\(cmd.name) — \(cmd.subtitle)", systemImage: cmd.icon)
                }
                .disabled(!cmd.isEnabled(session))
            }
        } label: {
            Image(systemName: "slash.circle")
                .frame(width: 32, height: 30)
                .contentShape(Rectangle())
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .help("Commands")
    }

    /// Live provider · model, switchable in place: every provider the agent
    /// knows, with its cached model list (server `use` command; in-memory,
    /// this session only).
    private var modelChip: some View {
        Menu {
            // Promote the live model to this agent's default, so it also runs
            // on it when woken for a group chat or delegated to — not just in
            // this chat (Agents tab does the same).
            Button {
                store.setAgentDefaultModel(session, provider: session.provider,
                                           model: session.model)
            } label: {
                Label(session.defaultModel == session.model
                      ? "Default: \(session.model)"
                      : "Set \(session.model) as default",
                      systemImage: "pin")
            }
            .disabled(session.model.isEmpty || session.model == "…"
                      || session.defaultModel == session.model)
            Divider()
            ForEach(session.knownProviders.keys.sorted(), id: \.self) { name in
                Section(name) {
                    let models = session.knownProviders[name] ?? []
                    if models.isEmpty {
                        Button("default model") {
                            session.use(provider: name, model: "")
                        }
                    }
                    ForEach(models, id: \.self) { model in
                        Button {
                            session.use(provider: name, model: model)
                        } label: {
                            if name == session.provider && model == session.model {
                                Label(model, systemImage: "checkmark")
                            } else {
                                Text(model)
                            }
                        }
                    }
                    Divider()
                    // The ready event only carries the built-in list; this
                    // pulls the provider's full live catalog.
                    Button {
                        session.refreshModels(provider: name)
                    } label: {
                        Label(session.modelsLoading
                              ? "Refreshing…" : "Refresh from API",
                              systemImage: "arrow.clockwise")
                    }
                    .disabled(session.modelsLoading)
                }
            }
        } label: {
            HStack(spacing: 5) {
                Image(systemName: "cpu")
                    .font(.callout)
                Text("\(session.provider) · \(session.model)")
                    .font(.callout)
                    .lineLimit(1)
            }
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(.quaternary.opacity(0.4), in: Capsule())
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .disabled(!session.isAlive || session.isRunning)
        .help("Switch model (this agent only)")
    }

    private var canSend: Bool {
        // session.processIsLive rather than isAlive: the flag lags behind a
        // process death (async termination handler) and the send path guards
        // too, but the composer shouldn't offer a send that can't go through.
        // The draft/staged check is the shared ComposerSendRules (NEXA-110).
        ComposerSendRules.canSend(
            draft: draft, staged: attachments.count,
            modeGate: session.processIsLive && !session.isRunning)
    }

    private func sendDraft() {
        // A bare "/command" runs an action instead of sending a message.
        if let cmd = SlashCommands.match(draft) {
            if cmd.isEnabled(session) { cmd.run(session) }
            draft = ""
            return
        }
        guard canSend else { return }
        var text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        if !attachments.isEmpty {
            if text.isEmpty {
                text = "Please review the attached files."
            }
            let list = attachments.map { "- \($0.path)" }.joined(separator: "\n")
            text += "\n\n[Attached files — read them with your tools]\n" + list
        }
        session.sendUserMessage(text)
        draft = ""
        attachments = []
    }
}

struct TranscriptRow: View {
    let item: TranscriptItem
    /// Ticket-link / @user decoration hook, passed down from ChatView so this
    /// row stays store-free (the openURL handler lives at ChatView's top
    /// level and covers the whole transcript).
    var decorate: ((AttributedString) -> AttributedString)? = nil
    /// When the row happened (nil hides the label; the shell passes item.ts).
    var ts: Date? = nil
    /// Show the HH:mm label under user/assistant bubbles (NEXA-118), the
    /// room transcript's timestamp placement.
    var showTimestamp = false
    @State private var expanded = false

    var body: some View {
        switch item.kind {
        case .user:
            HStack(alignment: .bottom) {
                Spacer(minLength: 60)
                VStack(alignment: .trailing, spacing: 2) {
                    MarkdownMessage(text: item.text, decorate: decorate)
                        .textSelection(.enabled)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(Comb.gold.opacity(0.22), in: RoundedRectangle(cornerRadius: 10))
                    if showTimestamp, let ts {
                        Text(TranscriptTime.timestamp(ts))
                            .font(.system(size: 9))
                            .foregroundStyle(.tertiary)
                    }
                }
            }
        case .assistant:
            VStack(alignment: .leading, spacing: 2) {
                MarkdownMessage(text: item.text)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if showTimestamp, let ts {
                    Text(TranscriptTime.timestamp(ts))
                        .font(.system(size: 9))
                        .foregroundStyle(.tertiary)
                }
            }
        case .toolCall:
            Label {
                Text(item.text)
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
            } icon: {
                Image(systemName: "circle.fill")
                    .font(.system(size: 8))
                    .foregroundStyle(.teal)
            }
        case .toolResult:
            let lines = item.text.split(separator: "\n", omittingEmptySubsequences: false)
            VStack(alignment: .leading, spacing: 4) {
                Text(expanded ? item.text : lines.prefix(6).joined(separator: "\n"))
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                if lines.count > 6 {
                    Button(expanded ? "Show less" : "Show all \(lines.count) lines") {
                        expanded.toggle()
                    }
                    .buttonStyle(.link)
                    .font(.caption)
                }
            }
            .padding(.leading, 18)
        case .nudge:
            Label(item.text, systemImage: "exclamationmark.triangle")
                .font(.caption)
                .foregroundStyle(.orange)
        case .log:
            Text(item.text)
                .font(.caption2)
                .foregroundStyle(.tertiary)
        case .error:
            Label(item.text, systemImage: "xmark.octagon")
                .font(.callout)
                .foregroundStyle(.red)
                .textSelection(.enabled)
        }
    }

}

/// Day-separator band between transcript rows (NEXA-118), shared with the
/// chat transcript: a Today / Yesterday / "EEEE, MMM d" label flanked by
/// hairlines, the room transcript's separator verbatim on `Date`.
struct TranscriptDaySeparator: View {
    let date: Date

    var body: some View {
        HStack(spacing: 8) {
            VStack { Divider() }
            Text(TranscriptTime.dayLabel(date))
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.tertiary)
            VStack { Divider() }
        }
        .padding(.vertical, 6)
    }
}

/// The agent asked for clarification (ask_user tool): one block per question,
/// selectable options plus a free-text override, mirroring the CLI's menus.
struct QuestionSheet: View {
    let questions: [AskQuestion]
    let submit: ([String]) -> Void

    @State private var selected: [UUID: Set<UUID>] = [:]
    @State private var freeText: [UUID: String] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(questions.count > 1 ? "The agent has questions" : "The agent has a question")
                .font(.title3.bold())
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    ForEach(questions) { q in
                        questionBlock(q)
                    }
                }
                .padding(.vertical, 2)
            }
            HStack {
                Spacer()
                Button("Skip") { submit(questions.map { _ in "" }) }
                Button("Answer") { submit(composedAnswers()) }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(20)
        .frame(width: 480)
        .frame(maxHeight: 560)
    }

    @ViewBuilder
    private func questionBlock(_ q: AskQuestion) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                if let header = q.header, !header.isEmpty {
                    Text(header.uppercased())
                        .font(.caption2.bold())
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(.tint.opacity(0.15), in: Capsule())
                }
                if q.multiSelect {
                    Text("pick any").font(.caption2).foregroundStyle(.secondary)
                }
            }
            Text(q.question).font(.body.weight(.medium))
            ForEach(q.options) { option in
                Button {
                    toggle(question: q, option: option)
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: isSelected(q, option)
                              ? (q.multiSelect ? "checkmark.square.fill" : "largecircle.fill.circle")
                              : (q.multiSelect ? "square" : "circle"))
                            .foregroundStyle(.tint)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(option.label)
                            if let detail = option.detail, !detail.isEmpty {
                                Text(detail).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            TextField(
                q.options.isEmpty ? "Your answer…" : "Or type your own answer…",
                text: Binding(
                    get: { freeText[q.id] ?? "" },
                    set: { freeText[q.id] = $0 }
                )
            )
            .textFieldStyle(.roundedBorder)
        }
    }

    private func isSelected(_ q: AskQuestion, _ option: AskOption) -> Bool {
        selected[q.id]?.contains(option.id) ?? false
    }

    private func toggle(question q: AskQuestion, option: AskOption) {
        var set = selected[q.id] ?? []
        if q.multiSelect {
            if set.contains(option.id) { set.remove(option.id) } else { set.insert(option.id) }
        } else {
            set = set.contains(option.id) ? [] : [option.id]
        }
        selected[q.id] = set
    }

    private func composedAnswers() -> [String] {
        questions.map { q in
            let typed = (freeText[q.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !typed.isEmpty { return typed }
            let chosen = q.options
                .filter { selected[q.id]?.contains($0.id) ?? false }
                .map(\.label)
            return chosen.joined(separator: ", ")
        }
    }
}

extension Int {
    var formattedTokens: String {
        self >= 10_000 ? "\(self / 1000)k"
            : self >= 1000 ? String(format: "%.1fk", Double(self) / 1000)
            : "\(self)"
    }
}
