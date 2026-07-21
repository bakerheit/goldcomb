import Foundation
import SwiftUI

/// One option offered by the agent's ask_user tool.
struct AskOption: Identifiable {
    let id = UUID()
    let label: String
    let detail: String?
}

/// One question from the agent's ask_user tool.
struct AskQuestion: Identifiable {
    let id = UUID()
    let question: String
    let header: String?
    let options: [AskOption]
    let multiSelect: Bool
}

/// One sub-agent deployed by this session's agent (the deploy_agent tool),
/// tracked from the subagent_start/subagent_end lifecycle events. Sub-agents
/// are transient: they live only in memory and die with the parent process.
struct SubAgentInfo: Identifiable {
    enum Status {
        case starting, running, completed, error

        /// Sidebar dot color: matches the CLI's status mapping.
        var color: Color {
            switch self {
            case .starting: return .gray
            case .running: return .blue
            case .completed: return .green
            case .error: return .red
            }
        }
    }

    /// The id minted by the backend for this sub-agent run.
    let id: String
    let label: String
    /// Task summary (truncated to 200 chars by the backend).
    let task: String
    var provider: String?
    var model: String?
    var status: Status = .starting
    /// Populated by subagent_end: "completed", "error", "step_limit", or
    /// "context_exhausted".
    var stopReason: String?
    var iterations: Int?
    var toolCalls: Int?
}

/// One agent = one `goldcomb --serve` process speaking NDJSON over stdio.
/// Multiple AgentSessions run concurrently, each fully isolated; sub-agents
/// deployed inside a session surface through the same event stream.
final class AgentSession: ObservableObject, Identifiable {
    /// Stable identity, persisted so the agent (and its project grouping)
    /// survives app relaunch.
    let id: UUID
    let name: String
    let directory: URL
    let sudoAtLaunch: Bool
    /// The agent's role — a single free-text field (the old persona enum and
    /// display role are unified into this). Shown in the sidebar AND passed as
    /// `--role` at launch, where it's injected into the system prompt (the
    /// names "planner"/"advisor" still carry rich built-in personas; anything
    /// else is used as-is). Changing it needs a restart to reach the process.
    @Published var role: String
    /// Free-form user-facing notes about this agent's responsibilities.
    /// Display-only, editable live.
    @Published var description: String
    /// Project this agent is grouped under in the sidebar, if any.
    var projectID: UUID?
    /// Parent in the project's agent tree (Agents tab), if any. nil = root.
    var parentID: UUID?
    /// Human-readable lead/peers/reports summary, computed from the tree at
    /// launch and passed as `--team` (a system-prompt block). Snapshot
    /// semantics: tree edits after launch apply on the next restart.
    var teamContext: String?
    /// The user-chosen default provider/model for this agent (Agents tab),
    /// passed as `--provider`/`--model` at launch so the agent runs on its own
    /// model whenever its process starts — including when it's woken for a
    /// group chat or delegated to, not just when the user opens its chat. nil =
    /// inherit the app's global default. Distinct from the live `provider`/
    /// `model` below, which reflect the running model and can be changed
    /// per-session from the chat model chip WITHOUT touching this default.
    @Published var defaultProvider: String?
    @Published var defaultModel: String?
    /// Set by the SessionStore; called when persisted state (provider/model)
    /// changes so the store can re-save. Never called before start().
    var onIdentityChange: (() -> Void)?

    @Published var transcript: [TranscriptItem] = []
    @Published var status: String? = nil          // spinner label, nil = idle
    @Published var isRunning = false              // a turn is in flight
    @Published var isAlive = false                // the process is up
    @Published var pendingConfirm: String? = nil  // tool summary awaiting approval
    @Published var pendingQuestions: [AskQuestion]? = nil  // ask_user in flight
    @Published var provider: String = "…"
    @Published var model: String = "…"
    @Published var knownProviders: [String: [String]] = [:]  // name → cached models
    @Published var modelsLoading = false  // a live catalog fetch is in flight
    @Published var readyConfigRevision = 0
    @Published var currentConfigRevision = 0
    var hasStaleConfig: Bool {
        isAlive && currentConfigRevision > 0 && readyConfigRevision != currentConfigRevision
    }
    @Published var sudo: Bool
    @Published var sessionIn = 0
    @Published var sessionOut = 0
    /// The saved thread backing this conversation ("chat id") — set once a
    /// turn persists one, or on resume; nil for a not-yet-saved chat.
    @Published var threadId: String? = nil
    /// Sub-agents this agent has deployed, in start order. Derived from live
    /// events only — never persisted; the list dies with the process.
    @Published var subagents: [SubAgentInfo] = []
    /// Read-only git working-tree state (NEXA-97), refreshed by the Project
    /// header's poll via `requestGitStatus()`. nil = not a git repo (or not
    /// yet fetched) — the header shows nothing git-related in that case.
    @Published var gitStatus: GitStatus? = nil
    /// True while a git_status command is awaiting its reply. The serve
    /// protocol's `error` event is generic, so this is how we tell a
    /// git-specific error (not-a-repo / git-missing → clear state, stay quiet)
    /// from any other error (surfaced in the transcript).
    private var gitStatusPending = false
    /// The diff sheet's pending state (NEXA-99): the last `git_diff` reply,
    /// or nil while a request is in flight / none has been made. Consumed by
    /// DiffView only — the sheet clears it (back to the loading state) when
    /// it dismisses. Single-slot: a new request replaces the previous reply.
    @Published var gitDiff: GitDiff? = nil
    /// The `error` event reply to a `git_diff` request (path outside the
    /// repo, etc.), shown inline in the sheet instead of the transcript.
    @Published var gitDiffError: String? = nil
    /// Same trick as gitStatusPending: the serve `error` event is generic, so
    /// this flag attributes an error to an in-flight git_diff request.
    private var gitDiffPending = false

    private var process: Process?
    /// The serve process's pid — sub-agent records carry their host pid, so
    /// this is how a deploy is matched back to the agent that ran it.
    var processID: Int32? { process?.processIdentifier }
    /// True only while the serve process is actually running. Unlike isAlive
    /// (flipped later by the async termination handler), this never lags a
    /// process death — the send path and the composer check it directly.
    var processIsLive: Bool { process?.isRunning == true }
    private var stdinHandle: FileHandle?
    private var stdoutBuffer = Data()
    private var stderrBuffer = Data()
    /// True while stop() is tearing the process down on purpose. stop()
    /// terminates the process, which fires the SAME terminationHandler as a
    /// crash — this flag is how the handler tells an intentional stop (stays
    /// silent) from an unexpected exit (surfaces an error + relaunch offer).
    private var userInitiatedStop = false
    /// Set once an unexpected exit has been surfaced (transcript error +
    /// relaunch offer). Offer-driven relaunch (relaunchAgent) clears it;
    /// start() re-arms it, so a fresh process that also crashes reports again.
    @Published var crashOffered = false

    init(id: UUID = UUID(), name: String, directory: URL, sudo: Bool,
         role: String = "", description: String = "",
         defaultProvider: String? = nil, defaultModel: String? = nil) {
        self.id = id
        self.name = name
        self.directory = directory
        self.sudoAtLaunch = sudo
        self.sudo = sudo
        self.role = role.trimmingCharacters(in: .whitespacesAndNewlines)
        self.description = description.trimmingCharacters(in: .whitespacesAndNewlines)
        self.defaultProvider = defaultProvider
        self.defaultModel = defaultModel
        NotificationCenter.default.addObserver(forName: .configRevisionChanged, object: nil,
                                                queue: .main) { [weak self] note in
            if let revision = note.object as? Int { self?.currentConfigRevision = revision }
        }
    }

    // MARK: - process lifecycle

    /// The `goldcomb --serve` argument list for this agent. Pure over the
    /// session's fields (given the configured base args), so the launch wiring
    /// — including the per-agent default model — is unit-testable without
    /// spawning a process.
    func serveArguments(baseArgs: [String]) -> [String] {
        // The session's name is its identity: scrum-ticket assignees and
        // thread history are stamped with it, so the Project tab can show
        // which agents are working on which tickets.
        var args = baseArgs + ["--serve", "--agent-name", name]
        // The unified free-text role: injected into the system prompt (the CLI
        // treats "planner"/"advisor" as rich personas, anything else as-is).
        if !role.isEmpty { args += ["--role", role] }
        if let teamContext { args += ["--team", teamContext] }
        // The agent's own default model, if the user set one — so it runs on
        // its configured model whenever the process starts (group chat,
        // delegation, or a plain open), not the app's global default.
        if let p = defaultProvider, !p.isEmpty { args += ["--provider", p] }
        if let m = defaultModel, !m.isEmpty { args += ["--model", m] }
        if sudoAtLaunch { args.append("--sudo") }
        return args
    }

    func start() {
        guard !isAlive else { return }  // relaunch path: never spawn a second process
        // Re-arm crash reporting for the new process; a user-initiated stop
        // preceding this start is over, so the next exit is unexpected again.
        userInitiatedStop = false
        crashOffered = false
        let parts = AppSettings.commandParts()
        guard let exe = parts.first else {
            append(.error, "No goldcomb command configured — set one in Settings.")
            return
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: exe)
        proc.arguments = serveArguments(baseArgs: Array(parts.dropFirst()))
        proc.currentDirectoryURL = directory

        let inPipe = Pipe(), outPipe = Pipe(), errPipe = Pipe()
        proc.standardInput = inPipe
        proc.standardOutput = outPipe
        proc.standardError = errPipe

        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.consume(handle.availableData, isStderr: false)
        }
        errPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.consume(handle.availableData, isStderr: true)
        }
        proc.terminationHandler = { [weak self] proc in
            // Captured BEFORE the main-hop: stop() nils self.process right
            // after terminate() while this handler fires async, so reading
            // terminationStatus/Reason on main could race a relaunch.
            let statusCode = proc.terminationStatus
            let reason = proc.terminationReason
            DispatchQueue.main.async {
                guard let self else { return }
                // Sub-agents live only in the exiting process's event stream,
                // so their rows must not leak into a later (re)start.
                self.subagents.removeAll()
                self.isAlive = false
                self.isRunning = false
                self.status = nil
                if self.userInitiatedStop {
                    // Intentional stop() — same handler as a crash, stays silent.
                    self.userInitiatedStop = false
                } else if reason == .exit && statusCode == 0 {
                    self.append(.log, "agent process exited")
                } else {
                    let detail = Self.exitDescription(status: statusCode, reason: reason)
                    self.append(.error, "Agent process \(detail) — the conversation above may be incomplete.")
                    self.crashOffered = true
                }
            }
        }

        do {
            try proc.run()
        } catch {
            append(.error, "Could not launch goldcomb: \(error.localizedDescription)")
            return
        }
        process = proc
        stdinHandle = inPipe.fileHandleForWriting
        isAlive = true
    }

    /// Human-readable cause of an unexpected process exit: non-zero exit
    /// codes and signal terminations (SIGSEGV, the OOM killer's SIGKILL, …).
    private static func exitDescription(status: Int32, reason: Process.TerminationReason) -> String {
        switch reason {
        case .exit:
            return "exited with code \(status)"
        case .uncaughtSignal:
            let names: [Int32: String] = [
                SIGABRT: "SIGABRT", SIGBUS: "SIGBUS", SIGSEGV: "SIGSEGV",
                SIGILL: "SIGILL", SIGTRAP: "SIGTRAP", SIGKILL: "SIGKILL",
            ]
            if let signal = names[status] {
                let cause = status == SIGKILL ? " (possibly killed by the system, e.g. out of memory)" : ""
                return "was terminated by \(signal)\(cause)"
            }
            return "was terminated by signal \(status)"
        @unknown default:
            return "exited unexpectedly (status \(status))"
        }
    }

    func stop() {
        userInitiatedStop = true
        send(["type": "exit"])
        process?.terminate()
        process = nil
    }

    /// Crash/exit recovery offered from the chat UI: start a fresh process,
    /// then resume the last thread (the threadId kept current by
    /// thread/turn_end/resumed events) so the conversation continues.
    func relaunchAgent() {
        crashOffered = false
        let resumeId = threadId
        start()
        if let resumeId {
            append(.log, "relaunching — resuming thread \(resumeId)")
            send(["type": "resume", "id": resumeId])
        } else {
            append(.log, "relaunching — no saved thread yet, starting fresh")
        }
    }

    /// SIGINT aborts the current turn, exactly like Ctrl-C in the terminal.
    func interrupt() {
        process?.interrupt()
    }

    // MARK: - commands to the agent

    func sendUserMessage(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        // isAlive alone is stale the moment the process dies (the flag flips
        // on the async termination handler) — check the process itself so a
        // send into a dead agent doesn't get recorded as a live turn.
        guard !trimmed.isEmpty, process?.isRunning == true else { return }
        append(.user, trimmed)
        isRunning = true
        send(["type": "user", "text": trimmed])
    }

    func respondToConfirm(_ decision: String) {
        // Record the outcome inline so the transcript shows how the
        // interruption ended even after the modal is gone.
        let summary = pendingConfirm ?? ""
        let truncated = summary.count > 80 ? String(summary.prefix(80)) + "…" : summary
        switch decision {
        case "yes":    append(.log, "approved: \(truncated)")
        case "always": append(.log, "approved (always): \(truncated)")
        case "no":     append(.log, "skipped: \(truncated)")
        case "abort":  append(.log, "aborted turn at: \(truncated)")
        default:       append(.log, "confirm → \(decision): \(truncated)")
        }
        pendingConfirm = nil
        send(["type": "confirm", "decision": decision])
    }

    func sendAnswers(_ answers: [String]) {
        // Record the outcome inline (see respondToConfirm). Blank answers are
        // skips — the QuestionSheet's Skip button sends all blanks.
        let count = answers.filter { !$0.isEmpty }.count
        if count == 0 {
            append(.log, "skipped \(answers.count) question(s)")
        } else {
            append(.log, "answered \(count)/\(answers.count) question(s)")
        }
        pendingQuestions = nil
        send(["type": "answer", "answers": answers])
    }

    /// Resume a saved thread from the project's .ai/threads history.
    func resumeThread(id: String) {
        guard isAlive, !isRunning else { return }
        send(["type": "resume", "id": id])
    }

    /// Switch the running session's provider/model (in-memory, per-agent).
    func use(provider: String, model: String) {
        var cmd: [String: Any] = ["type": "use", "provider": provider]
        if !model.isEmpty { cmd["model"] = model }
        send(cmd)
    }

    /// Start a fresh conversation: history drops, the next turn opens a new
    /// thread. The transcript clears when the server confirms.
    func clearConversation() {
        send(["type": "clear"])
    }

    func setSudo(_ on: Bool) {
        sudo = on
        send(["type": "sudo", "on": on])
    }

    /// Ask the server to live-fetch a provider's full model catalog; the reply
    /// (a `models` event) updates `knownProviders`. Defaults to the current
    /// provider. The ready event only carries the built-in list, so this is how
    /// the picker gets everything the provider actually offers.
    func refreshModels(provider: String? = nil) {
        guard isAlive else { return }
        modelsLoading = true
        var cmd: [String: Any] = ["type": "models"]
        if let provider { cmd["provider"] = provider }
        send(cmd)
    }

    /// Summarize the conversation in place to shrink context (mirrors the CLI
    /// /compact). Unlike clearing, the thread is kept and history continues
    /// from the summary. The server replies with a `compacted` event.
    func compact() {
        guard isAlive, !isRunning else { return }
        isRunning = true  // a model call is about to run; block the composer
        append(.log, "compacting the conversation…")
        send(["type": "compact"])
    }

    /// Turn per-project scrum tracking on/off (creates the board on first on).
    func setScrumEnabled(_ on: Bool) {
        send(["type": "scrum", "on": on])
    }

    /// Run one scrum-board action (add/move/edit a ticket) straight through
    /// the agent process — no model turn involved. The reply arrives as a
    /// `scrum_result` event; the board re-reads the file on its timer.
    func scrumAction(_ action: String, fields: [String: Any] = [:]) {
        var cmd: [String: Any] = ["type": "scrum_action", "action": action]
        cmd.merge(fields) { _, new in new }
        send(cmd)
    }

    func use(provider: String, model: String?) {
        var cmd: [String: Any] = ["type": "use", "provider": provider]
        if let model, !model.isEmpty { cmd["model"] = model }
        send(cmd)
    }

    /// Ask the server for read-only git working-tree status (NEXA-97). Handled
    /// outside the turn loop (like `threads`), so it is safe to poll while the
    /// agent is mid-turn or idle. The reply arrives as a `git_status` event, or
    /// an `error` event for not-a-repo / git-missing (which clears gitStatus).
    /// Called from the Project header's existing 2s reload tick — no dedicated
    /// polling loop. A no-op when the process is down (nothing to poll).
    func requestGitStatus() {
        guard process?.isRunning == true else { return }
        gitStatusPending = true
        send(["type": "git_status"])
    }

    /// Ask the server for one file's diff (NEXA-99), staged or unstaged.
    /// Out-of-band like `git_status` — safe while the agent is mid-turn. The
    /// reply arrives as a `git_diff` event (or `error`); firing a new request
    /// immediately puts consumers back into the loading state by clearing
    /// the previous reply.
    func requestGitDiff(path: String, staged: Bool) {
        guard process?.isRunning == true else { return }
        gitDiff = nil
        gitDiffError = nil
        gitDiffPending = true
        send(["type": "git_diff", "path": path, "staged": staged])
    }

    private func send(_ object: [String: Any]) {
        // Writing to a dead process's stdin pipe raises
        // NSFileHandleOperationException — NOT catchable from Swift, a hard
        // crash on this @MainActor path. The isAlive flag lags (flipped by
        // the async termination handler), so check the process itself and
        // surface the same error + relaunch offer as a detected crash.
        guard process?.isRunning == true else {
            guard !crashOffered else { return }
            crashOffered = true
            isAlive = false
            isRunning = false
            status = nil
            append(.error, "Agent process is not running — message not sent.")
            return
        }
        guard let handle = stdinHandle,
              let data = try? JSONSerialization.data(withJSONObject: object)
        else { return }
        handle.write(data)
        handle.write(Data("\n".utf8))
    }

    // MARK: - event stream

    private func consume(_ data: Data, isStderr: Bool) {
        guard !data.isEmpty else { return }
        if isStderr {
            stderrBuffer.append(data)
            drainLines(&stderrBuffer) { line in
                DispatchQueue.main.async { self.append(.log, line) }
            }
        } else {
            stdoutBuffer.append(data)
            drainLines(&stdoutBuffer) { line in
                guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)),
                      let event = obj as? [String: Any] else { return }
                DispatchQueue.main.async { self.handle(event) }
            }
        }
    }

    private func drainLines(_ buffer: inout Data, _ each: (String) -> Void) {
        while let nl = buffer.firstIndex(of: UInt8(ascii: "\n")) {
            let lineData = buffer[buffer.startIndex..<nl]
            buffer = Data(buffer[buffer.index(after: nl)...])
            if let line = String(data: lineData, encoding: .utf8),
               !line.trimmingCharacters(in: .whitespaces).isEmpty {
                each(line)
            }
        }
    }

    private func handle(_ event: [String: Any]) {
        switch event["event"] as? String {
        case "ready":
            provider = event["provider"] as? String ?? "?"
            model = event["model"] as? String ?? "?"
            readyConfigRevision = event["config_revision"] as? Int ?? 0
            currentConfigRevision = max(currentConfigRevision, readyConfigRevision)
            if let providers = event["providers"] as? [String: [String: Any]] {
                knownProviders = providers.mapValues { ($0["models"] as? [String]) ?? [] }
            }
            onIdentityChange?()
        case "status":
            status = event["label"] as? String
        case "message_start":
            transcript.append(TranscriptItem(kind: .assistant, text: ""))
        case "delta":
            if let text = event["text"] as? String,
               let last = transcript.indices.last,
               transcript[last].kind == .assistant {
                transcript[last].text += text
            }
        case "message_end":
            if let text = event["text"] as? String,
               let last = transcript.indices.last,
               transcript[last].kind == .assistant {
                transcript[last].text = text
            }
        case "tool_call":
            append(.toolCall, event["summary"] as? String ?? "")
        case "tool_result":
            append(.toolResult, event["output"] as? String ?? "")
        case "nudge":
            append(.nudge, event["text"] as? String ?? "")
        case "confirm_request":
            pendingConfirm = event["summary"] as? String ?? "(tool call)"
            // Inline record of the interruption itself: the alert is
            // ephemeral, so this line is what scrolling back shows.
            append(.log, "approval requested: \(pendingConfirm ?? "")")
        case "ask_request":
            let raw = event["questions"] as? [[String: Any]] ?? []
            pendingQuestions = raw.compactMap { q in
                guard let text = q["question"] as? String, !text.isEmpty else { return nil }
                let options = ((q["options"] as? [[String: Any]]) ?? []).compactMap { o -> AskOption? in
                    guard let label = o["label"] as? String, !label.isEmpty else { return nil }
                    return AskOption(label: label, detail: o["description"] as? String)
                }
                return AskQuestion(
                    question: text,
                    header: q["header"] as? String,
                    options: options,
                    multiSelect: q["multi_select"] as? Bool ?? false
                )
            }
            if let questions = pendingQuestions {
                append(.log, questions.count == 1
                    ? "asked a question: \(questions[0].question)"
                    : "asked \(questions.count) questions")
            }
        case "usage", "turn_end":
            sessionIn = event["session_in"] as? Int ?? sessionIn
            sessionOut = event["session_out"] as? Int ?? sessionOut
            if event["event"] as? String == "turn_end" {
                isRunning = false
                status = nil
                if let tid = event["thread_id"] as? String {
                    threadId = tid
                }
            }
        case "thread":
            if let tid = event["thread_id"] as? String {
                threadId = tid
            }
        case "cleared":
            transcript.removeAll()
            threadId = nil
            append(.log, "new conversation")
        case "compacted":
            // The compact command isn't a turn, so nothing resets isRunning
            // for us — do it here (compact() set it to block the composer).
            isRunning = false
            status = nil
            if event["ok"] as? Bool == true {
                let before = event["before"] as? Int ?? 0
                append(.log, "compacted \(before) messages → a summary")
            } else {
                let reason = event["reason"] as? String ?? "unchanged"
                append(.log, reason == "too-short"
                       ? "nothing to compact yet — the conversation is short"
                       : "compaction produced no summary; history unchanged")
            }
        case "models":
            modelsLoading = false
            if let name = event["provider"] as? String,
               let models = event["models"] as? [String] {
                knownProviders[name] = models
            }
            if event["ok"] as? Bool == false,
               let err = event["error"] as? String {
                append(.log, "couldn't fetch models: \(err)")
            }
        case "using":
            provider = event["provider"] as? String ?? provider
            model = event["model"] as? String ?? model
            onIdentityChange?()
        case "sudo":
            sudo = event["on"] as? Bool ?? sudo
        case "scrum":
            append(.log, event["message"] as? String ?? "scrum setting changed")
        case "scrum_result":
            // A GUI board edit was applied (or refused). Log it in the chat
            // transcript only when it failed — the board UI shows successes.
            if event["ok"] as? Bool == false {
                append(.log, event["message"] as? String ?? "scrum action failed")
            }
        case "resumed":
            if let tid = event["thread_id"] as? String {
                threadId = tid
                hydrateTranscript(threadId: tid)
                append(.log, "resumed \(event["title"] as? String ?? tid)")
            }
        case "interrupted":
            isRunning = false
            status = nil
            append(.log, "interrupted")
        case "subagent_start":
            guard let id = event["id"] as? String, !id.isEmpty else { break }
            subagents.append(SubAgentInfo(
                id: id,
                label: event["label"] as? String ?? "agent",
                task: event["task"] as? String ?? "",
                provider: event["provider"] as? String,
                model: event["model"] as? String,
                status: .running
            ))
        case "subagent_end":
            guard let id = event["id"] as? String,
                  let idx = subagents.lastIndex(where: { $0.id == id }) else { break }
            let reason = event["stop_reason"] as? String
            subagents[idx].stopReason = reason
            subagents[idx].status = reason == "error" ? .error : .completed
            subagents[idx].iterations = event["iterations"] as? Int
            subagents[idx].toolCalls = event["tool_calls"] as? Int
        case "git_status":
            gitStatus = GitStatus.decode(event: event)
            gitStatusPending = false
        case "git_diff":
            gitDiff = GitDiff.decode(event: event)
            gitDiffError = gitDiff == nil ? "Malformed diff reply from server." : nil
            gitDiffPending = false
        case "error":
            // The serve `error` event is generic. If a git_status request is
            // in flight, this is a not-a-repo / git-missing error — clear the
            // git state and stay quiet rather than surfacing it in the chat.
            if gitStatusPending {
                gitStatusPending = false
                gitStatus = nil
                return
            }
            // Same attribution for git_diff (NEXA-99): surface it inline in
            // the diff sheet, not in the chat transcript.
            if gitDiffPending {
                gitDiffPending = false
                gitDiffError = event["message"] as? String ?? "unknown error"
                return
            }
            append(.error, event["message"] as? String ?? "unknown error")
        default:
            break
        }
    }

    private func append(_ kind: TranscriptItem.Kind, _ text: String) {
        guard !text.isEmpty else { return }
        transcript.append(TranscriptItem(kind: kind, text: text))
    }

    /// Rebuild the visible transcript from the resumed thread's interchange
    /// file (.ai/threads/<id>.jsonl) so the prior conversation is on screen.
    /// Parsing lives in TranscriptItem.fromThreadFile (unit-testable there);
    /// each message's `timestamp` lands on the row's `ts` (NEXA-118).
    private func hydrateTranscript(threadId: String) {
        let url = directory.appendingPathComponent(".ai/threads/\(threadId).jsonl")
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return }
        let items = TranscriptItem.fromThreadFile(text)
        if !items.isEmpty {
            transcript = items
        }
    }
}
