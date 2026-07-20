import SwiftUI
import UniformTypeIdentifiers

struct ChatView: View {
    @ObservedObject var session: AgentSession
    @State private var draft = ""
    @State private var showHistory = false
    @State private var historyThreads: [ThreadSummary] = []
    @State private var attachments: [URL] = []
    @State private var showFilePicker = false
    @State private var dropTargeted = false
    @State private var pickedProvider = ""
    @State private var pickedModel = ""
    @State private var showQuestionSheet = false

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
                    historyThreads = ThreadSummary
                        .loadAll(projectDir: session.directory)
                        // Only this agent's own threads: legacy aliases count
                        // as the same identity, sub-agent threads don't.
                        .filter { AgentIdentity.matches(session.name, headerAgent: $0.agent)
                                  && !AgentIdentity.isSubagent($0.agent) }
                    showHistory = true
                } label: {
                    Label("History", systemImage: "clock.arrow.circlepath")
                }
                .help("This agent's past conversations")
                .popover(isPresented: $showHistory, arrowEdge: .bottom) {
                    historyPopover
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
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    ForEach(session.transcript) { item in
                        TranscriptRow(item: item)
                            .id(item.id)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: session.transcript.count) {
                if let last = session.transcript.last {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
            .onChange(of: session.transcript.last?.text) {
                if let last = session.transcript.last {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
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
        VStack(spacing: 6) {
            if !attachments.isEmpty {
                attachmentChips
            }
            HStack(spacing: 8) {
                TextField("Message the agent…", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...6)
                    .onSubmit(sendDraft)
                if session.isRunning {
                    Button {
                        session.interrupt()
                    } label: {
                        Image(systemName: "stop.circle.fill")
                            .font(.system(size: 28))
                            .foregroundStyle(.red)
                            .frame(width: 36, height: 36)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Stop this turn")
                } else {
                    Button(action: sendDraft) {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 28))
                            .frame(width: 36, height: 36)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(!canSend)
                    .help("Send (Return)")
                }
            }
            utilityRow
        }
        .padding(10)
        .fileImporter(
            isPresented: $showFilePicker,
            allowedContentTypes: [.item],
            allowsMultipleSelection: true
        ) { result in
            if case .success(let urls) = result {
                attachments += urls.filter { !attachments.contains($0) }
            }
        }
        .onDrop(of: [.fileURL], isTargeted: $dropTargeted) { providers in
            for provider in providers {
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    guard let url else { return }
                    DispatchQueue.main.async {
                        if !attachments.contains(url) { attachments.append(url) }
                    }
                }
            }
            return true
        }
        .background(dropTargeted ? Comb.gold.opacity(0.08) : Color.clear)
    }

    /// Files queued for the next message, shown as removable chips. Paths are
    /// appended to the message — the agent reads them with its own tools.
    private var attachmentChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(attachments, id: \.self) { url in
                    HStack(spacing: 4) {
                        Image(systemName: "doc")
                            .font(.caption)
                        Text(url.lastPathComponent)
                            .font(.caption)
                            .lineLimit(1)
                        Button {
                            attachments.removeAll { $0 == url }
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.caption)
                                .foregroundStyle(.tertiary)
                        }
                        .buttonStyle(.plain)
                        .help("Remove")
                    }
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(Comb.gold.opacity(0.12), in: Capsule())
                }
            }
        }
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

    /// Live provider · model, switchable in place: every provider the agent
    /// knows, with its cached model list (server `use` command; in-memory,
    /// this session only).
    private var modelChip: some View {
        Menu {
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
        session.processIsLive && !session.isRunning
            && (!draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                || !attachments.isEmpty)
    }

    private func sendDraft() {
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
    @State private var expanded = false

    var body: some View {
        switch item.kind {
        case .user:
            HStack {
                Spacer(minLength: 60)
                Text(item.text)
                    .textSelection(.enabled)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(.tint.opacity(0.15), in: RoundedRectangle(cornerRadius: 10))
            }
        case .assistant:
            Text(markdown(item.text))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
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

    private func markdown(_ text: String) -> AttributedString {
        (try? AttributedString(
            markdown: text,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )) ?? AttributedString(text)
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
