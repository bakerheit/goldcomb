import Foundation
import SwiftUI

/// App-level settings, persisted in UserDefaults.
enum AppSettings {
    static let commandKey = "goldcombCommand"

    /// The command that starts one agent: an executable plus arguments;
    /// `--serve` is appended by the session. Defaults to running the module
    /// from this repo's virtualenv.
    static var command: String {
        get {
            guard var stored = UserDefaults.standard.string(forKey: commandKey) else {
                return defaultCommand
            }
            // Pre-rename installs stored a nexais command; migrate in place.
            if stored.contains("nexais") {
                stored = stored
                    .replacingOccurrences(of: "/nexais/", with: "/goldcomb/")
                    .replacingOccurrences(of: "-m nexais", with: "-m goldcomb")
                UserDefaults.standard.set(stored, forKey: commandKey)
            }
            return stored
        }
        set { UserDefaults.standard.set(newValue, forKey: commandKey) }
    }

    static var defaultCommand: String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return "\(home)/workspace/goldcomb/.venv/bin/python -m goldcomb"
    }

    struct CommandInvocation {
        let executable: String
        let arguments: [String]
    }

    /// Parse a shell-like command without invoking a shell. Quoted paths keep
    /// spaces intact and Process receives an explicit executable/argv array.
    static func commandInvocation() throws -> CommandInvocation {
        var parts: [String] = [], current = "", quote: Character?
        var escaped = false
        for char in command {
            if escaped { current.append(char); escaped = false; continue }
            if char == "\\" { escaped = true; continue }
            if let active = quote {
                if char == active { quote = nil } else { current.append(char) }
            } else if char == "\"" || char == "'" {
                quote = char
            } else if char.isWhitespace {
                if !current.isEmpty { parts.append(current); current = "" }
            } else { current.append(char) }
        }
        if quote != nil { throw NSError(domain: "GoldcombSettings", code: 1,
            userInfo: [NSLocalizedDescriptionKey: "Unclosed quote in agent command"] ) }
        if !current.isEmpty { parts.append(current) }
        guard let executable = parts.first else { throw NSError(domain: "GoldcombSettings", code: 2,
            userInfo: [NSLocalizedDescriptionKey: "No Goldcomb command configured"]) }
        return CommandInvocation(executable: executable, arguments: Array(parts.dropFirst()))
    }

    static func commandParts() -> [String] {
        guard let invocation = try? commandInvocation() else { return [] }
        return [invocation.executable] + invocation.arguments
    }
}

/// Sidebar selection: an agent, a project header (selecting one shows its
/// agents and focuses its most recent one), or a chat room (NEXA-66/71 —
/// rooms live in the sidebar beside agents, not only in the Chats tab).
enum SidebarItem: Hashable {
    case project(UUID)
    case agent(UUID)
    case chat(String)  // room id
}

// MARK: - persistence
//
// Projects and agents are written to `SidebarState.json` inside the app's
// Application Support directory as pretty JSON on every mutation, and
// restored at startup. Only organizational state lives here — transcripts
// stay in each project's .ai/threads/*.jsonl as before.

/// The JSON shape of `SidebarState.json` (versioned for future migrations).
private struct SidebarState: Codable {
    var version: Int = 1
    var projects: [SavedProject] = []
    var agents: [SavedAgent] = []
}

private struct SavedProject: Codable {
    var id: UUID
    var name: String
    var directory: String  // absolute path
}

struct SavedAgent: Codable {
    var id: UUID
    var name: String
    var directory: String  // absolute path (the agent's cwd)
    var sudo: Bool = false
    var role: String = ""          // unified free-text role (see migration below)
    var description: String = ""
    var provider: String? = nil  // last used, if the process reported it
    var model: String? = nil
    var projectID: UUID? = nil   // nil = ungrouped
    var parentID: UUID? = nil    // agent-tree parent (Agents tab); nil = root

    private enum CodingKeys: String, CodingKey {
        // `personaRole` is read for migration only; never written back.
        case id, name, directory, sudo, role, description, personaRole
        case provider, model, projectID, parentID
    }

    init(id: UUID, name: String, directory: String, sudo: Bool = false,
         role: String = "", description: String = "",
         provider: String? = nil, model: String? = nil,
         projectID: UUID? = nil, parentID: UUID? = nil) {
        self.id = id; self.name = name; self.directory = directory; self.sudo = sudo
        self.role = role; self.description = description
        self.provider = provider; self.model = model
        self.projectID = projectID; self.parentID = parentID
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(UUID.self, forKey: .id)
        name = try c.decode(String.self, forKey: .name)
        directory = try c.decode(String.self, forKey: .directory)
        sudo = try c.decodeIfPresent(Bool.self, forKey: .sudo) ?? false
        description = try c.decodeIfPresent(String.self, forKey: .description) ?? ""
        provider = try c.decodeIfPresent(String.self, forKey: .provider)
        model = try c.decodeIfPresent(String.self, forKey: .model)
        projectID = try c.decodeIfPresent(UUID.self, forKey: .projectID)
        parentID = try c.decodeIfPresent(UUID.self, forKey: .parentID)
        // Migration: role and persona are now one free-text field. Prefer the
        // free-text display role; fall back to the old persona (planner/advisor
        // still resolve to their rich block via the CLI). "worker" was the
        // no-op default persona, so it maps to no role.
        let savedRole = try c.decodeIfPresent(String.self, forKey: .role) ?? ""
        var persona = try c.decodeIfPresent(String.self, forKey: .personaRole) ?? ""
        if persona == "worker" { persona = "" }
        role = savedRole.isEmpty ? persona : savedRole
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(name, forKey: .name)
        try c.encode(directory, forKey: .directory)
        try c.encode(sudo, forKey: .sudo)
        try c.encode(role, forKey: .role)
        try c.encode(description, forKey: .description)
        try c.encodeIfPresent(provider, forKey: .provider)
        try c.encodeIfPresent(model, forKey: .model)
        try c.encodeIfPresent(projectID, forKey: .projectID)
        try c.encodeIfPresent(parentID, forKey: .parentID)
        // personaRole is intentionally not written — it's unified into role.
    }
}

/// Owns every project and running agent session.
final class SessionStore: ObservableObject {
    @Published var projects: [Project] = []
    @Published var sessions: [AgentSession] = []
    @Published var selection: SidebarItem? = nil
    /// A ticket a chat link asked to jump to (NEXA-84). ProjectDetailView
    /// watches this: when set, it switches to the Sprint tab and hands the id
    /// to SprintTabView to surface. Cleared once consumed.
    @Published var pendingTicketFocus: String? = nil
    /// Bumped when a room is marked read. Unread counts live in UserDefaults
    /// (not @Published), so the sidebar badge would otherwise not re-diff when
    /// it should clear — the same NSTableView re-diff path as NEXA-43/44. The
    /// sidebar folds this into its pulse snapshot; see ContentView.
    @Published var chatReadTick = 0
    /// Projects whose agent rows are folded away in the sidebar.
    @Published var collapsed: Set<UUID> = []
    /// Whether the right-side file explorer is visible; persisted.
    @Published var explorerVisible: Bool {
        didSet { UserDefaults.standard.set(explorerVisible, forKey: "explorerVisible") }
    }
    /// One explorer per agent, created lazily; agents in one project share the
    /// folder, so their explorers look identical.
    var explorers: [UUID: FileExplorerModel] = [:]

    /// Guards against re-saving while restore() is mid-flight.
    private var isRestoring = false

    /// Deployed sub-agents per project (from .ai/agents records), polled by
    /// a store-lifetime timer — row views must NOT poll themselves: a
    /// ForEach that renders zero rows never fires onAppear/onReceive, so a
    /// self-polling empty section stays empty forever.
    @Published var subAgents: [UUID: [SubAgentRecord]] = [:]
    private var subAgentTimer: Timer?

    /// Agent chat rooms per project (.ai/chats), shared with the same poll
    /// timer. The app is the only process connected to every agent, so it is
    /// also the delivery broker: new messages wake the addressed agents.
    @Published var chats: [UUID: [ChatRoom]] = [:]
    /// Delivery cursor per "chatID#agentName": how many messages that agent
    /// has been shown. Persisted so a relaunch doesn't re-deliver history.
    private var chatCursors: [String: Int] =
        UserDefaults.standard.dictionary(forKey: "chatCursors") as? [String: Int] ?? [:]

    init() {
        explorerVisible = UserDefaults.standard.object(forKey: "explorerVisible") as? Bool ?? true
        restore()
        pollSubAgents()
        let timer = Timer(timeInterval: 2.0, repeats: true) { [weak self] _ in
            DispatchQueue.main.async { self?.pollSubAgents() }
        }
        // .common mode: keep polling during scroll/menu/popover tracking,
        // where .default-mode timers silently pause.
        RunLoop.main.add(timer, forMode: .common)
        subAgentTimer = timer
    }

    /// Test-only store: skips `restore()` (real disk), the sub-agent poll
    /// timer, and any process launch. `save()` still runs but only logs on
    /// failure, so sessions pointed at a temp directory leave real sidebar
    /// state intact. Lets unit tests exercise the pure tree/promotion state
    /// logic (the NEXA-38/43/44 regression surface) without side effects.
    /// Internal + `@testable`-only by convention — production uses `init()`.
    init(forTesting: Bool) {
        explorerVisible = UserDefaults.standard.object(forKey: "explorerVisible") as? Bool ?? true
        // Deliberately no restore(), no pollSubAgents(), no timer.
    }

    private func pollSubAgents() {
        var next: [UUID: [SubAgentRecord]] = [:]
        var nextChats: [UUID: [ChatRoom]] = [:]
        for project in projects {
            let records = SubAgentRecord.loadAll(projectDir: project.directory)
            if !records.isEmpty { next[project.id] = records }
            let rooms = ChatRoom.loadAll(projectDir: project.directory)
            if !rooms.isEmpty { nextChats[project.id] = rooms }
        }
        if next != subAgents { subAgents = next }
        if nextChats != chats { chats = nextChats }
        promoteDeploys(next)
        deliverChats(nextChats)
    }

    /// A user post from the Chats tab should show up immediately, not on the
    /// next 2s tick.
    func refreshChatsNow() { pollSubAgents() }

    /// The project a chat room belongs to, matched by directory (rooms carry
    /// their project path in `projectDir`). Compare normalized *paths*, not
    /// URLs: `deletingLastPathComponent` leaves a trailing slash, so URL
    /// equality against the project's slash-free directory would never match.
    func projectID(forRoom room: ChatRoom) -> UUID? {
        let target = room.projectDir.standardizedFileURL.path
        return projects.first {
            $0.directory.standardizedFileURL.path == target
        }?.id
    }

    /// Every room across projects, for resolving a `.chat(id)` selection.
    func room(withID id: String) -> ChatRoom? {
        for rooms in chats.values {
            if let hit = rooms.first(where: { $0.id == id }) { return hit }
        }
        return nil
    }

    /// Route the reader to a ticket's Sprint view (NEXA-84): select its
    /// project, then flag the ticket so ProjectDetailView opens the board.
    func focusTicket(_ ticket: String, in projectID: UUID?) {
        if let pid = projectID { selection = .project(pid) }
        pendingTicketFocus = ticket
    }

    /// Mark a room read and nudge the sidebar so its unread badge clears now,
    /// not on the next poll that happens to republish `chats`.
    func markChatRead(_ room: ChatRoom) {
        ChatReadState.markRead(room)
        chatReadTick += 1
    }

    // MARK: chat delivery broker

    /// Wake each addressed agent with the messages it hasn't seen. Rules:
    /// wait for a burst to go quiet so one turn digests it all; never hand an
    /// agent its own words; skip busy agents (the cursor holds, so they catch
    /// up next tick); and once a room hits its unattended cap, stop — the
    /// discussion waits for the user, which is what keeps two chatty agents
    /// from ping-ponging forever on the user's API bill.
    private func deliverChats(_ byProject: [UUID: [ChatRoom]]) {
        let now = Date().timeIntervalSince1970
        var dirty = false
        for (projectID, rooms) in byProject {
            for room in rooms {
                guard let last = room.messages.last else { continue }
                for name in room.participants where name != "user" {
                    let key = "\(room.id)#\(name)"
                    if chatCursors[key] == nil, now - room.lastActivity > 3600 {
                        // First sight of a long-quiet room (fresh install,
                        // cleared defaults): don't wake everyone over history.
                        chatCursors[key] = room.messages.count
                        dirty = true
                        continue
                    }
                    let cursor = min(chatCursors[key] ?? 0, room.messages.count)
                    guard room.messages.count > cursor else { continue }
                    let pending = Array(room.messages[cursor...])
                    let foreign = pending.filter { $0.from != name }
                    if foreign.isEmpty {
                        // Only their own posts — nothing to say to them.
                        chatCursors[key] = room.messages.count
                        dirty = true
                        continue
                    }
                    if !room.addresses(name, in: foreign) {
                        // Not for them. Advance the cursor anyway: an
                        // unaddressed message is *delivered as read*, not
                        // queued, or it would arrive later bundled with
                        // something that is addressed to them.
                        chatCursors[key] = room.messages.count
                        dirty = true
                        continue
                    }
                    guard now - last.ts > 4 else { continue }   // burst debounce
                    if room.isPaused { continue }               // user's floor
                    guard let session = sessions.first(where: {
                        $0.projectID == projectID && $0.name == name
                    }), session.isAlive, !session.isRunning,
                        session.pendingConfirm == nil,
                        session.pendingQuestions == nil
                    else { continue }
                    session.sendUserMessage(
                        chatDigest(room: room, messages: foreign, recipient: name,
                                   tagged: room.taggedAgents(in: foreign)))
                    chatCursors[key] = room.messages.count
                    dirty = true
                }
            }
        }
        if dirty {
            UserDefaults.standard.set(chatCursors, forKey: "chatCursors")
        }
    }

    private func chatDigest(room: ChatRoom, messages: [ChatMessage],
                            recipient: String, tagged: [String]) -> String {
        let lines = messages.suffix(12).map { m -> String in
            let who = m.isUser ? "user (the human)" : m.from
            // Attached files are invisible to the agent unless the digest
            // names them — without this line a delivered file is a silent
            // no-op (NEXA-75).
            let atts = m.attachments.map { "\n  " + $0.digestLine }.joined()
            return "\(who): \(m.text)\(atts)"
        }
        // Framing depends on whether — and whom — the message @-tagged. A
        // tagged agent is expected to answer; an untagged one is a bystander
        // who may chime in; an untagged broadcast is open to everyone.
        let short = { (n: String) in n.split(separator: "(").first.map(String.init)?
            .trimmingCharacters(in: .whitespaces) ?? n }
        let stance: String
        if tagged.contains(recipient) {
            stance = "You (\(short(recipient))) were tagged — a reply is expected of you. "
        } else if !tagged.isEmpty {
            let who = tagged.map(short).joined(separator: ", ")
            stance = "\(who) was tagged, not you — chime in only if you have "
                + "something specific to add. "
        } else {
            stance = "This is an open message to the room — respond if you have "
                + "something to add. "
        }
        return """
        [chat \(room.id)] New in "\(room.title)" \
        (participants: \(room.participants.joined(separator: ", "))):
        \(lines.joined(separator: "\n"))

        \(stance)Reply with the chat tool (action='post', id='\(room.id)'); \
        action='read' shows the full history. If you have nothing to add, \
        don't post — just say so in one line and stop.

        Tag the teammates you expect an answer from with @name; a tagged \
        agent is expected to reply, and others may still chime in.
        """
    }

    /// Promotions the user has explicitly undone (closed the agent row while
    /// its deploy record was still around) — don't resurrect those. Persisted
    /// across launches: promotion is now permanent (see promoteDeploys), so a
    /// removed agent must stay removed, not re-promote from its lingering
    /// on-disk record on the next launch.
    private var declinedPromotions: Set<String> = Set(
        UserDefaults.standard.stringArray(forKey: "declinedPromotions") ?? [])

    private func decline(_ key: String) {
        declinedPromotions.insert(key)
        UserDefaults.standard.set(Array(declinedPromotions),
                                  forKey: "declinedPromotions")
    }

    private func promotionKey(_ projectID: UUID, _ label: String) -> String {
        "\(projectID.uuidString)#\(label)"
    }

    /// Every deployed worker becomes a permanent, configurable roster member:
    /// the identity (name, memory file, thread history, roster entry) already
    /// exists — promotion gives that person a chattable session, parented under
    /// whichever agent deployed them, so the user can see and configure it (its
    /// default model, etc.). Once promoted it persists like any agent; the user
    /// removes it explicitly if they don't want it (recorded in
    /// declinedPromotions). No age window: a deploy the user ran hours ago still
    /// joins the roster.
    private func promoteDeploys(_ byProject: [UUID: [SubAgentRecord]]) {
        for (projectID, records) in byProject {
            guard let project = projects.first(where: { $0.id == projectID })
            else { continue }
            for record in records {
                if declinedPromotions.contains(promotionKey(projectID, record.label)) {
                    continue
                }
                let exists = sessions.contains {
                    $0.name == record.label && $0.projectID == projectID
                }
                if exists { continue }
                let deployer = sessions.first {
                    guard let pid = $0.processID else { return false }
                    return Int(pid) == record.pid
                }
                createSession(
                    name: record.label,
                    directory: project.directory,
                    sudo: false,
                    parentID: deployer?.id,
                    select: false,   // never steal focus mid-work
                    in: project
                )
            }
        }
    }

    /// Test seam: runs the promotion pass over an explicit record set, without
    /// going through the disk-reading `pollSubAgents`. Internal so `@testable`
    /// suites can pin the NEXA-38 skip-logic (stale / declined / duplicate)
    /// directly. Live promotion still calls `createSession` → `start()`, so
    /// tests only exercise the skip paths, never a real spawn.
    func runPromoteDeploysForTesting(_ byProject: [UUID: [SubAgentRecord]]) {
        promoteDeploys(byProject)
    }

    // MARK: persistence

    /// `~/Library/Application Support/Goldcomb/SidebarState.json`, honoring
    /// the sandbox container when running as an .app bundle.
    private var stateFileURL: URL {
        let base = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask
        ).first ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support")
        let bundleID = Bundle.main.bundleIdentifier ?? "Goldcomb"
        return base.appendingPathComponent(bundleID)
            .appendingPathComponent("SidebarState.json")
    }

    /// Adopt SidebarState.json from the pre-rename bundle id's folder so an
    /// upgraded install keeps its projects and agents. Copy, not move — an
    /// old build left around can still read its own state.
    private func migrateLegacyStateIfNeeded() {
        let url = stateFileURL
        guard !FileManager.default.fileExists(atPath: url.path) else { return }
        let legacy = url.deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("dev.nexais.app")
            .appendingPathComponent("SidebarState.json")
        guard FileManager.default.fileExists(atPath: legacy.path) else { return }
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? FileManager.default.copyItem(at: legacy, to: url)
    }

    /// Write projects + agents to disk (atomic via a temp file + replace).
    /// Every mutating store method calls this; failures only log.
    private func save() {
        guard !isRestoring else { return }
        let state = SidebarState(
            projects: projects.map {
                SavedProject(id: $0.id, name: $0.name, directory: $0.directory.path)
            },
            agents: sessions.map {
                var agent = SavedAgent(id: $0.id, name: $0.name,
                                       directory: $0.directory.path,
                                       sudo: $0.sudoAtLaunch, role: $0.role,
                                       description: $0.description,
                                       projectID: $0.projectID,
                                       parentID: $0.parentID)
                // Persist the user-chosen DEFAULT (Agents tab), not the live
                // running model — a live model change from the chat chip is a
                // per-session override and must not become the saved default.
                agent.provider = $0.defaultProvider
                agent.model = $0.defaultModel
                return agent
            }
        )
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let data = try encoder.encode(state)
            let url = stateFileURL
            try FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try data.write(to: url, options: .atomic)
        } catch {
            NSLog("goldcomb: could not persist sidebar state: \(error.localizedDescription)")
        }
        writeAgentConfigs()
    }

    /// Publish each project's per-agent default models to
    /// `<project>/.ai/agents/agent-config.json`, so the deploy flow
    /// (goldcomb/agents.py `configured_default`) launches a deployed agent on
    /// the model the user chose for it. Keyed by the agent's name (the deploy
    /// side also matches the bare label inside "Name (label)"). Written next to
    /// the sidebar state so app and CLI stay in step.
    private func writeAgentConfigs() {
        for project in projects {
            var entries: [String: [String: String]] = [:]
            for a in sessions where a.projectID == project.id {
                guard let model = a.defaultModel, !model.isEmpty else { continue }
                entries[a.name] = ["provider": a.defaultProvider ?? "",
                                   "model": model]
            }
            let dir = project.directory.appendingPathComponent(".ai/agents")
            let url = dir.appendingPathComponent("agent-config.json")
            if entries.isEmpty {
                try? FileManager.default.removeItem(at: url)  // nothing configured
                continue
            }
            let payload: [String: Any] = ["version": 1, "agents": entries]
            guard let data = try? JSONSerialization.data(
                withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
            else { continue }
            try? FileManager.default.createDirectory(
                at: dir, withIntermediateDirectories: true)
            try? data.write(to: url, options: .atomic)
        }
    }

    /// Reload the saved sidebar and relaunch each agent where it left off.
    /// Missing/unreadable state just means "start empty"; malformed files
    /// are backed up aside rather than destroying anything. An agent whose
    /// saved project vanished falls back to folder matching, then ungrouped
    /// (the project row stays saved, so it rejoins if the project returns).
    private func restore() {
        migrateLegacyStateIfNeeded()
        let url = stateFileURL
        guard let data = try? Data(contentsOf: url) else { return }
        guard let state = try? JSONDecoder().decode(SidebarState.self, from: data) else {
            NSLog("goldcomb: sidebar state unreadable, starting empty " +
                  "(backed up to \(url.lastPathComponent).bad)")
            try? data.write(to: url.deletingLastPathComponent()
                .appendingPathComponent(url.lastPathComponent + ".bad"))
            return
        }

        isRestoring = true
        projects = state.projects.map {
            Project(id: $0.id, name: $0.name,
                    directory: URL(fileURLWithPath: $0.directory))
        }
        for saved in state.agents {
            let session = AgentSession(
                id: saved.id,
                name: saved.name,
                directory: URL(fileURLWithPath: saved.directory),
                sudo: saved.sudo,
                role: saved.role,
                description: saved.description,
                defaultProvider: saved.provider,
                defaultModel: saved.model
            )
            let savedProject = saved.projectID.flatMap { id in
                projects.first { $0.id == id }
            }
            session.projectID = savedProject?.id
                ?? matchingProject(for: session.directory)?.id
            session.parentID = saved.parentID
            sessions.append(session)
        }
        // Drop tree edges whose parent no longer exists, then hand every
        // agent its team snapshot before launch.
        let ids = Set(sessions.map(\.id))
        for session in sessions where session.parentID != nil {
            if !ids.contains(session.parentID!) { session.parentID = nil }
        }
        sessions.forEach { $0.teamContext = teamContext(for: $0) }
        isRestoring = false

        sessions.forEach { $0.start() }
        selection = sessions.last.map { .agent($0.id) }
            ?? projects.last.map { .project($0.id) }
    }

    /// The explorer for the project the current selection belongs to.
    var activeExplorer: FileExplorerModel? {
        guard let session = selectedSession else { return nil }
        if let existing = explorers[session.id] { return existing }
        let model = FileExplorerModel(directory: session.directory)
        explorers[session.id] = model
        return model
    }

    func createProject(name: String, directory: URL) -> Project {
        let n = name.trimmingCharacters(in: .whitespaces)
        let project = Project(
            name: n.isEmpty ? directory.lastPathComponent : n,
            directory: directory
        )
        projects.append(project)
        selection = .project(project.id)
        save()
        return project
    }

    /// Rename via the store so the change is persisted to disk.
    func rename(_ project: Project, to name: String) {
        project.name = name
        save()
    }

    @discardableResult
    func createSession(name: String, directory: URL, sudo: Bool,
                       role: String = "", description: String = "",
                       parentID: UUID? = nil,
                       select: Bool = true,
                       in project: Project? = nil) -> AgentSession {
        let n = name.trimmingCharacters(in: .whitespaces)
        let session = AgentSession(
            name: n.isEmpty ? Names.random(avoiding: Set(sessions.map(\.name))) : n,
            directory: directory,
            sudo: sudo,
            role: role,
            description: description
        )
        session.projectID = project?.id ?? matchingProject(for: directory)?.id
        session.parentID = parentID
        sessions.append(session)
        session.teamContext = teamContext(for: session)
        if select { selection = .agent(session.id) }
        save()
        // Persist provider/model as soon as the process reports them so the
        // saved state reflects what the agent actually runs.
        session.onIdentityChange = { [weak self] in self?.save() }
        session.start()
        return session
    }

    /// Set an agent's default model (Agents tab / "set as default"). Persists
    /// it so the agent launches on this model in future — including when woken
    /// for a group chat or delegated to. If the agent is running, it's applied
    /// live too (a `use` command) so it takes effect without a restart; an
    /// empty provider clears the default (back to the app's global default).
    func setAgentDefaultModel(_ session: AgentSession,
                              provider: String, model: String) {
        let p = provider.trimmingCharacters(in: .whitespaces)
        session.defaultProvider = p.isEmpty ? nil : p
        session.defaultModel = model.trimmingCharacters(in: .whitespaces).isEmpty
            ? nil : model.trimmingCharacters(in: .whitespaces)
        save()
        if session.isAlive, let dp = session.defaultProvider {
            session.use(provider: dp, model: session.defaultModel ?? "")
        }
    }

    /// Edit an agent's display metadata (role/description) from the config
    /// sheet. These are app-side only (not passed to the process), so the
    /// change is live — just persist it.
    func updateAgentMetadata(_ session: AgentSession, role: String,
                             description: String) {
        session.role = role.trimmingCharacters(in: .whitespacesAndNewlines)
        session.description = description.trimmingCharacters(in: .whitespacesAndNewlines)
        save()
    }

    // MARK: agent tree (Agents tab)

    /// Children of an agent in its project's tree, stable-ordered by name.
    func children(of id: UUID) -> [AgentSession] {
        sessions.filter { $0.parentID == id }
            .sorted { $0.name.localizedCompare($1.name) == .orderedAscending }
    }

    /// Root agents (no parent) among the given project-scoped sessions.
    func treeRoots(among scoped: [AgentSession]) -> [AgentSession] {
        let ids = Set(scoped.map(\.id))
        return scoped
            .filter { $0.parentID == nil || !ids.contains($0.parentID!) }
            .sorted { $0.name.localizedCompare($1.name) == .orderedAscending }
    }

    /// The lead/peers/reports summary passed to an agent at launch (--team).
    /// Factual only — behavioral guidance lives in the role prompt.
    func teamContext(for session: AgentSession) -> String? {
        let parent = session.parentID.flatMap { pid in
            sessions.first { $0.id == pid }
        }
        let peers = parent.map { children(of: $0.id) } ?? []
        let reports = children(of: session.id)
        var lines: [String] = []
        if let parent {
            let role = parent.role.isEmpty ? "" : " (role: \(parent.role))"
            lines.append("Your lead: @\(parent.name)\(role).")
        }
        let peerNames = peers.filter { $0.id != session.id }.map { "@\($0.name)" }
        if !peerNames.isEmpty {
            lines.append("Your teammates: \(peerNames.joined(separator: ", ")).")
        }
        if !reports.isEmpty {
            let names = reports.map { r in
                r.role.isEmpty ? "@\(r.name)" : "@\(r.name) (\(r.role))"
            }
            lines.append("Your reports: \(names.joined(separator: ", ")).")
        }
        return lines.isEmpty ? nil : lines.joined(separator: " ")
    }

    /// Reparent an agent (drag & drop on the org chart). Refuses self and
    /// descendant targets — a lead dropped under its own report would cycle.
    /// nil parent = promote to root. Team snapshots refresh for the next
    /// (re)start of each agent.
    func reparent(_ session: AgentSession, under parent: AgentSession?) {
        guard session.id != parent?.id else { return }
        var cursor = parent
        while let c = cursor {
            if c.id == session.id { return }   // would create a cycle
            cursor = sessions.first { $0.id == c.parentID }
        }
        guard session.parentID != parent?.id else { return }
        objectWillChange.send()
        session.parentID = parent?.id
        sessions.forEach { $0.teamContext = teamContext(for: $0) }
        save()
    }

    /// Remove an agent from the tree: its children move up to its parent
    /// (nothing is orphaned), the process stops, and the state persists.
    func removeFromTree(_ session: AgentSession) {
        for child in children(of: session.id) {
            child.parentID = session.parentID
        }
        remove(session)
    }

    func sessionsFor(_ project: Project) -> [AgentSession] {
        sessions.filter { $0.projectID == project.id }
    }

    /// The project's agent that should run board/thread actions from the
    /// project view: the most recent live one, else the most recent.
    func actionSession(for project: Project) -> AgentSession? {
        let mine = sessionsFor(project)
        return mine.last(where: { $0.isAlive }) ?? mine.last
    }

    func toggleCollapsed(_ project: Project) {
        if collapsed.contains(project.id) {
            collapsed.remove(project.id)
        } else {
            collapsed.insert(project.id)
        }
    }

    /// The session shown in the detail pane for the current selection.
    var selectedSession: AgentSession? {
        switch selection {
        case .agent(let id):
            return sessions.first { $0.id == id }
        case .project(let id):
            return sessions.last { $0.projectID == id }
        case .chat, nil:
            // A chat room selection has no backing agent session — the detail
            // pane routes to ChatRoomView, not through here.
            return nil
        }
    }

    func remove(_ session: AgentSession) {
        // If a deploy record still matches this row, closing it is the user
        // saying "no thanks" to the promotion — remember that, or the poller
        // would recreate the session two seconds from now.
        if let projectID = session.projectID,
           subAgents[projectID]?.contains(where: { $0.label == session.name }) == true {
            decline(promotionKey(projectID, session.name))
        }
        session.stop()
        sessions.removeAll { $0.id == session.id }
        explorers.removeValue(forKey: session.id)
        switch selection {
        case .agent(let id) where id == session.id:
            // The removed agent was selected: fall back to a sibling agent,
            // else select the project itself (so the detail pane never shows
            // a dead agent).
            let siblings = sessions.filter { $0.projectID == session.projectID }
            selection = siblings.last.map { .agent($0.id) }
                ?? session.projectID.map { .project($0) }
        case .project(let id) where id == session.projectID:
            // The project stays selected, but `selectedSession` derives from
            // "the project's last agent" — re-point the selection so SwiftUI
            // refreshes the detail pane (a removed last agent would otherwise
            // leave the project view bound to a stale session).
            selection = .project(id)
        default:
            break
        }
        save()
    }

    /// Removes the project from the sidebar and stops/closes its agents.
    /// Nothing is deleted from disk — the folder and its files are untouched
    /// (only the app's saved sidebar state forgets the grouping).
    func removeProject(_ project: Project) {
        sessionsFor(project).forEach {
            $0.stop()
            explorers.removeValue(forKey: $0.id)
        }
        sessions.removeAll { $0.projectID == project.id }
        projects.removeAll { $0.id == project.id }
        if selection == .project(project.id) {
            selection = projects.last.map { .project($0.id) }
        } else if case .agent(let id) = selection,
                  !sessions.contains(where: { $0.id == id }) {
            selection = projects.last.map { .project($0.id) }
        }
        save()
    }

    /// The project a session directory belongs to (NEXA-114: ChatView's
    /// ticket-link handler routes via this, like rooms do via
    /// `projectID(forRoom:)`). Thin public wrapper over `matchingProject`.
    func projectID(forDirectory directory: URL) -> UUID? {
        matchingProject(for: directory)?.id
    }

    private func matchingProject(for directory: URL) -> Project? {
        projects.first { $0.directory == directory }
    }

    func shutdownAll() {
        sessions.forEach { $0.stop() }
    }
}
