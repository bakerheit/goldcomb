import SwiftUI

// MARK: - .ai folder models
// The project's vendor-neutral AI workspace: any tool can write these files;
// we render whatever is there. Board: .ai/scrum/board.json (legacy fallback
// .nexais/board.json). Threads: .ai/threads/<id>.jsonl.

struct ScrumComment: Identifiable {
    let id = UUID()
    let who: String
    let text: String
    let at: Double
}

struct ScrumTask: Identifiable {
    let id: String
    let title: String
    // var: board views overlay optimistic status changes while a move is
    // in flight (the file re-read confirms it a moment later).
    var status: String
    let points: Int
    let assignee: String?
    let labels: [String]
    let blockedBy: [String]
    let notes: String
    let comments: [ScrumComment]
    /// Epoch seconds when the task entered `done` (nil for older boards);
    /// the Done lane sorts on this, falling back to `created`.
    let doneAt: Double?
    let created: Double?
    var commentCount: Int { comments.count }
}

struct ScrumStory: Identifiable {
    let id: String
    let title: String
    let priority: String
    let tasks: [ScrumTask]
}

struct ScrumEpicGroup: Identifiable {
    let id: String
    let title: String
    let stories: [ScrumStory]
}

struct ScrumSprint {
    let number: Int
    let goal: String
    let active: Bool
    let storyIds: [String]
}

struct ScrumBoard {
    let project: String
    let groups: [ScrumEpicGroup]
    let sprint: ScrumSprint?

    var allTasks: [ScrumTask] { groups.flatMap { $0.stories.flatMap(\.tasks) } }

    func statusCount(_ status: String) -> Int {
        allTasks.filter { $0.status == status }.count
    }

    /// (done, total) story points across the sprint's stories.
    var sprintPoints: (done: Int, total: Int) {
        guard let sprint else { return (0, 0) }
        let stories = groups.flatMap(\.stories).filter { sprint.storyIds.contains($0.id) }
        let tasks = stories.flatMap(\.tasks)
        return (
            tasks.filter { $0.status == "done" }.reduce(0) { $0 + $1.points },
            tasks.reduce(0) { $0 + $1.points }
        )
    }

    static func load(projectDir: URL) -> ScrumBoard? {
        let candidates = [
            projectDir.appendingPathComponent(".ai/scrum/board.json"),
            projectDir.appendingPathComponent(".nexais/board.json"),  // legacy (pre-rename)
        ]
        guard let url = candidates.first(where: { FileManager.default.fileExists(atPath: $0.path) }),
              let data = try? Data(contentsOf: url),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let storiesRaw = root["stories"] as? [String: [String: Any]]
        else { return nil }

        func story(_ sid: String, _ s: [String: Any]) -> ScrumStory {
            let tasks = ((s["tasks"] as? [[String: Any]]) ?? []).compactMap { t -> ScrumTask? in
                guard let tid = t["id"] as? String else { return nil }
                let who = (t["assignee"] as? String)?
                    .trimmingCharacters(in: .whitespaces)
                let comments = ((t["comments"] as? [[String: Any]]) ?? []).map {
                    ScrumComment(
                        who: $0["who"] as? String ?? "?",
                        text: $0["text"] as? String ?? "",
                        at: $0["at"] as? Double ?? 0
                    )
                }
                return ScrumTask(
                    id: tid,
                    title: t["title"] as? String ?? "",
                    status: t["status"] as? String ?? "todo",
                    points: t["points"] as? Int ?? 0,
                    assignee: (who?.isEmpty == false) ? who : nil,
                    labels: t["labels"] as? [String] ?? [],
                    blockedBy: t["blocked_by"] as? [String] ?? [],
                    notes: t["notes"] as? String ?? "",
                    comments: comments,
                    doneAt: t["done_at"] as? Double,
                    created: t["created"] as? Double
                )
            }
            return ScrumStory(
                id: sid,
                title: s["title"] as? String ?? sid,
                priority: s["priority"] as? String ?? "medium",
                tasks: tasks
            )
        }

        var remaining = storiesRaw
        var groups: [ScrumEpicGroup] = []
        for (eid, epic) in (root["epics"] as? [String: [String: Any]]) ?? [:] {
            let sids = (epic["stories"] as? [String]) ?? []
            let stories = sids.compactMap { sid -> ScrumStory? in
                guard let s = remaining.removeValue(forKey: sid) else { return nil }
                return story(sid, s)
            }
            groups.append(ScrumEpicGroup(
                id: eid, title: epic["title"] as? String ?? eid, stories: stories
            ))
        }
        groups.sort { $0.id < $1.id }
        if !remaining.isEmpty {
            let loose = remaining.map { story($0.key, $0.value) }.sorted { $0.id < $1.id }
            groups.append(ScrumEpicGroup(id: "_none", title: "No epic", stories: loose))
        }

        var sprint: ScrumSprint?
        if let s = root["sprint"] as? [String: Any] {
            sprint = ScrumSprint(
                number: s["number"] as? Int ?? 0,
                goal: s["goal"] as? String ?? "",
                active: s["active"] as? Bool ?? false,
                storyIds: (s["stories"] as? [String]) ?? []
            )
        }
        let meta = root["meta"] as? [String: Any]
        return ScrumBoard(
            project: meta?["project"] as? String ?? "",
            groups: groups,
            sprint: sprint
        )
    }
}

struct ThreadSummary: Identifiable {
    let id: String
    let title: String
    let updated: String
    let agent: String
    let provider: String?
    let model: String?
    let messageCount: Int

    static func loadAll(projectDir: URL) -> [ThreadSummary] {
        let dir = projectDir.appendingPathComponent(".ai/threads")
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: nil
        ) else { return [] }
        var out: [ThreadSummary] = []
        for url in files where url.pathExtension == "jsonl" {
            guard let text = try? String(contentsOf: url, encoding: .utf8) else { continue }
            let lines = text.split(separator: "\n", omittingEmptySubsequences: true)
            guard let first = lines.first,
                  let header = (try? JSONSerialization.jsonObject(
                      with: Data(first.utf8))) as? [String: Any],
                  header["type"] as? String == "thread",
                  let id = header["id"] as? String
            else { continue }
            out.append(ThreadSummary(
                id: id,
                title: header["title"] as? String ?? "(untitled)",
                updated: header["updated"] as? String ?? "",
                agent: header["agent"] as? String ?? "?",
                provider: header["provider"] as? String,
                model: header["model"] as? String,
                messageCount: lines.count - 1
            ))
        }
        return out.sorted { $0.updated > $1.updated }
    }
}

// MARK: - the Sprint tab

/// The project's ticket board, full-page: sprint banner, who-is-working list,
/// and the kanban columns. Live-refreshed — agents (this one or any other
/// tool) move tickets as they work.
struct SprintTabView: View {
    @ObservedObject var session: AgentSession
    @EnvironmentObject var store: SessionStore

    @State private var board: ScrumBoard?
    private let refresh = Timer.publish(every: 2, on: .main, in: .common).autoconnect()

    @State private var showStartSprint = false
    @State private var confirmEndSprint = false
    @State private var sprintGoal = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let board {
                    if let sprint = board.sprint {
                        sprintBanner(board: board, sprint: sprint)
                    }
                    HStack(spacing: 10) {
                        sprintControls(board)
                        plannerPanel
                        advisorPanel
                    }
                    activeWork(board)
                    ScrumBoardView(board: board, session: session)
                } else {
                    emptyState
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onAppear(perform: reload)
        .onReceive(refresh) { _ in reload() }
        .sheet(isPresented: $showStartSprint) {
            VStack(alignment: .leading, spacing: 16) {
                Text("Start sprint").font(.title3.bold())
                TextField("Sprint goal", text: $sprintGoal)
                HStack {
                    Spacer()
                    Button("Cancel") { showStartSprint = false }
                        .keyboardShortcut(.cancelAction)
                    Button("Start") {
                        session.scrumAction("sprint_start", fields: ["goal": sprintGoal])
                        sprintGoal = ""
                        showStartSprint = false
                    }
                    .keyboardShortcut(.defaultAction)
                }
            }
            .padding(20)
            .frame(width: 380)
        }
        .confirmationDialog(
            "End the active sprint?", isPresented: $confirmEndSprint
        ) {
            Button("End sprint", role: .destructive) {
                session.scrumAction("sprint_end")
            }
        } message: {
            Text("Unfinished stories stay on the board and are listed as carry-over.")
        }
    }

    /// Start/end sprint from the app — previously agent-only.
    @ViewBuilder
    private func sprintControls(_ board: ScrumBoard) -> some View {
        HStack(spacing: 10) {
            if board.sprint?.active == true {
                Button("End sprint…") { confirmEndSprint = true }
                    .disabled(!session.isAlive)
            } else {
                Button("Start sprint…") { showStartSprint = true }
                    .disabled(!session.isAlive)
                    .help("Begin a numbered sprint; add stories from a card's context menu")
            }
            Spacer()
        }
    }

    // MARK: role quick actions

    /// The project's planner: a dedicated scrum-master agent (`--role
    /// planner`) that grooms this board, plans sprints, and reports. One per
    /// project; the quick actions message it and jump to its chat.
    private var plannerSession: AgentSession? {
        let mine = store.sessions.filter { candidate in
            candidate.personaRole == "planner" &&
            (session.projectID != nil
                ? candidate.projectID == session.projectID
                : candidate.directory == session.directory)
        }
        return mine.first(where: \.isAlive) ?? mine.first
    }

    /// The project's advisor: a dedicated finance/business agent (`--role
    /// advisor`) that tracks costs, budgets, and accounting setup.
    private var advisorSession: AgentSession? {
        let mine = store.sessions.filter { candidate in
            candidate.personaRole == "advisor" &&
            (session.projectID != nil
                ? candidate.projectID == session.projectID
                : candidate.directory == session.directory)
        }
        return mine.first(where: \.isAlive) ?? mine.first
    }

    @ViewBuilder
    private var plannerPanel: some View {
        HStack(spacing: 8) {
            Label("Planner", systemImage: "person.text.rectangle")
                .font(.callout.weight(.semibold))
                .foregroundStyle(.secondary)
            if let planner = plannerSession {
                let busy = planner.isRunning || !planner.isAlive
                Button("Standup") {
                    ask(planner,
                        "Standup report, please: read the board (show, "
                        + "sprint_status, history) and summarize what moved "
                        + "recently, what's in progress and by whom, what's "
                        + "blocked, and what you recommend next. Don't change "
                        + "the board.")
                }
                .disabled(busy)
                Button("Groom backlog") {
                    ask(planner,
                        "Groom the backlog: read the board, then tidy it — "
                        + "break down oversized stories, add missing points "
                        + "and labels, question stale in_progress tickets, "
                        + "and file tickets for obvious gaps. Summarize every "
                        + "change you make.")
                }
                .disabled(busy)
                Button("Plan sprint") {
                    ask(planner,
                        "Plan the next sprint: review the open stories, "
                        + "propose a coherent sprint goal, then sprint_start "
                        + "with that goal and add the right stories. Explain "
                        + "the plan in a few lines.")
                }
                .disabled(busy)
                if planner.isRunning {
                    ProgressView().controlSize(.small)
                }
            } else {
                Button("Create planner agent") {
                    let project = store.projects.first { $0.id == session.projectID }
                    store.createSession(
                        name: Names.random(
                            avoiding: Set(store.sessions.map(\.name))),
                        directory: session.directory,
                        sudo: false, personaRole: "planner",
                        in: project)
                }
                .help("A scrum-master agent that owns this board: grooming, "
                      + "sprint planning, and standup reports")
            }
        }
    }

    @ViewBuilder
    private var advisorPanel: some View {
        HStack(spacing: 8) {
            Label("Advisor", systemImage: "chart.line.uptrend.xyaxis")
                .font(.callout.weight(.semibold))
                .foregroundStyle(.secondary)
            if let advisor = advisorSession {
                let busy = advisor.isRunning || !advisor.isAlive
                Button("Cost report") {
                    ask(advisor,
                        "Cost report, please: read memory and "
                        + ".ai/finance/ledger.md if present, summarize "
                        + "monthly spend and burn rate by category, flag "
                        + "missing data, and recommend next tracking steps. "
                        + "Don't change product code.")
                }
                .disabled(busy)
                Button("Budget check") {
                    ask(advisor,
                        "Budget check, please: compare current and expected "
                        + "spend to known budgets or thresholds from memory "
                        + "and .ai/finance/ledger.md if present, flag "
                        + "overruns and risks, and suggest accounting or "
                        + "bookkeeping setup next steps. Don't change "
                        + "product code.")
                }
                .disabled(busy)
                if advisor.isRunning {
                    ProgressView().controlSize(.small)
                }
            } else {
                Button("Create advisor agent") {
                    let project = store.projects.first { $0.id == session.projectID }
                    store.createSession(
                        name: Names.random(
                            avoiding: Set(store.sessions.map(\.name))),
                        directory: session.directory,
                        sudo: false, personaRole: "advisor",
                        in: project)
                }
                .help("A finance/business agent that tracks project costs, "
                      + "budgets, and accounting setup")
            }
        }
    }

    private func ask(_ agent: AgentSession, _ prompt: String) {
        agent.sendUserMessage(prompt)
        store.selection = .agent(agent.id)
    }

    private func reload() {
        board = ScrumBoard.load(projectDir: session.directory)
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Ticket tracking is off for this project.", systemImage: "checklist")
                .font(.callout)
                .foregroundStyle(.secondary)
            Button("Enable ticket tracking") {
                session.setScrumEnabled(true)
            }
            .disabled(!session.isAlive)
            .help("Creates .ai/scrum/board.json and offers the scrum tool "
                  + "to agents working in this project")
        }
        .padding(.top, 8)
    }

    private func sprintBanner(board: ScrumBoard, sprint: ScrumSprint) -> some View {
        let points = board.sprintPoints
        return VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Sprint \(sprint.number)").font(.headline)
                if sprint.active {
                    Text("ACTIVE")
                        .font(.caption2.bold())
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(.green.opacity(0.2), in: Capsule())
                } else {
                    Text("ended").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if points.total > 0 {
                    Text("\(points.done)/\(points.total) pts")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            if !sprint.goal.isEmpty {
                Text(sprint.goal).font(.callout)
            }
            if points.total > 0 {
                ProgressView(value: Double(points.done), total: Double(points.total))
            }
        }
        .padding(12)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
    }

    /// Which agents are on which tickets right now: every in-progress task,
    /// grouped under its assignee. Assignees matching an agent running in
    /// this app get a live dot.
    @ViewBuilder
    private func activeWork(_ board: ScrumBoard) -> some View {
        let active = board.allTasks.filter { $0.status == "in_progress" }
        if !active.isEmpty {
            let liveAgents = Set(store.sessions.filter(\.isAlive).map(\.name))
            let grouped = Dictionary(grouping: active) { $0.assignee ?? "unassigned" }
            VStack(alignment: .leading, spacing: 6) {
                Text("Working now").font(.subheadline.bold())
                ForEach(grouped.keys.sorted(), id: \.self) { who in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        if liveAgents.contains(who) {
                            Circle().fill(.green).frame(width: 7, height: 7)
                        }
                        Text("@\(who)")
                            .font(.callout.weight(.semibold))
                            .foregroundStyle(liveAgents.contains(who) ? .primary : .secondary)
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(grouped[who] ?? []) { task in
                                HStack(spacing: 6) {
                                    Text(task.id)
                                        .font(.caption.monospaced().bold())
                                        .foregroundStyle(.orange)
                                    Text(task.title).font(.callout).lineLimit(1)
                                }
                            }
                        }
                    }
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
        }
    }
}

// MARK: - the Backlog tab

/// Stories not yet pulled into the active sprint (finished ones drop off) —
/// the work that's waiting. Grouped by epic; one click sends a story into
/// the running sprint.
struct BacklogTabView: View {
    @ObservedObject var session: AgentSession
    @EnvironmentObject var store: SessionStore

    @State private var board: ScrumBoard?
    @State private var detail: ScrumBoardView.DetailTarget? = nil
    @State private var newTicket: ScrumBoardView.NewTicketTarget? = nil
    private let refresh = Timer.publish(every: 2, on: .main, in: .common).autoconnect()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let board {
                    let groups = backlogGroups(board)
                    header(board, groups: groups)
                    if groups.isEmpty {
                        ContentUnavailableView(
                            "Backlog is clear",
                            systemImage: "tray",
                            description: Text("Every open story is in the "
                                              + "sprint. Create a new ticket "
                                              + "to add unscheduled work.")
                        )
                        .frame(maxWidth: .infinity, minHeight: 220)
                    } else {
                        ForEach(groups) { group in
                            epicSection(group, board: board)
                        }
                    }
                } else {
                    Text("Ticket tracking is off for this project — enable "
                         + "it from the Sprint tab.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(.top, 8)
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onAppear(perform: reload)
        .onReceive(refresh) { _ in reload() }
        .sheet(item: $detail) { target in
            TicketDetailSheet(task: target.task, storyId: target.storyId,
                              session: session)
        }
        .sheet(item: $newTicket) { target in
            NewTicketSheet(epic: target.epic) { title, priority, points in
                session.createTicket(title: title, priority: priority,
                                     points: points, epic: target.epic)
            }
        }
    }

    private func reload() {
        board = ScrumBoard.load(projectDir: session.directory)
    }

    /// A story is backlog when it is not in the active sprint and not fully
    /// done (finished work is history, not backlog).
    private func backlogGroups(_ board: ScrumBoard) -> [ScrumEpicGroup] {
        let sprintIds = Set(board.sprint?.active == true
                            ? board.sprint!.storyIds : [])
        return board.groups.compactMap { group in
            let stories = group.stories.filter { story in
                if sprintIds.contains(story.id) { return false }
                let tasks = story.tasks
                let allDone = !tasks.isEmpty && tasks.allSatisfy { $0.status == "done" }
                return !allDone
            }
            return stories.isEmpty ? nil
                : ScrumEpicGroup(id: group.id, title: group.title, stories: stories)
        }
    }

    private func header(_ board: ScrumBoard,
                        groups: [ScrumEpicGroup]) -> some View {
        let stories = groups.flatMap(\.stories)
        let points = stories.flatMap(\.tasks).reduce(0) { $0 + $1.points }
        return HStack(spacing: 8) {
            Label("Backlog", systemImage: "tray.full")
                .font(.headline)
            Text("\(stories.count) stories · \(points) pts unscheduled")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            Menu {
                ForEach(board.groups) { group in
                    Button(group.id == "_none" ? "No epic" : "\(group.id) — \(group.title)") {
                        newTicket = ScrumBoardView.NewTicketTarget(
                            epic: group.id == "_none" ? nil : group)
                    }
                }
                Divider()
                Button("New epic first…") {
                    newTicket = ScrumBoardView.NewTicketTarget(epic: nil)
                }
            } label: {
                Label("New ticket", systemImage: "plus")
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .disabled(!session.isAlive)
            .help("Create an unscheduled story with its first task")
            if board.sprint?.active == true {
                Text("Sprint \(board.sprint!.number) is running — pull "
                     + "stories in as capacity allows")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
        }
    }

    private func epicSection(_ group: ScrumEpicGroup,
                             board: ScrumBoard) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(group.id == "_none" ? "No epic" : group.title)
                    .font(.subheadline.bold())
                if group.id != "_none" {
                    Text(group.id)
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                }
            }
            ForEach(group.stories) { story in
                storyRow(story, board: board)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
    }

    private func storyRow(_ story: ScrumStory, board: ScrumBoard) -> some View {
        let done = story.tasks.filter { $0.status == "done" }
            .reduce(0) { $0 + $1.points }
        let total = story.tasks.reduce(0) { $0 + $1.points }
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(story.id)
                    .font(.caption.monospaced().bold())
                    .foregroundStyle(Comb.amber)
                Text(story.title)
                    .font(.callout)
                    .lineLimit(1)
                if story.priority == "high" {
                    Image(systemName: "exclamationmark")
                        .font(.caption2.bold())
                        .foregroundStyle(.red)
                }
                Spacer()
                if total > 0 {
                    Text("\(done)/\(total)pt")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                if board.sprint?.active == true {
                    Button("Add to sprint") {
                        session.scrumAction("sprint_add", fields: ["story": story.id])
                    }
                    .font(.caption)
                    .disabled(!session.isAlive)
                    .help("Pull this story into sprint \(board.sprint!.number)")
                }
            }
            if !story.tasks.isEmpty {
                HStack(spacing: 6) {
                    ForEach(story.tasks) { task in
                        Button {
                            detail = ScrumBoardView.DetailTarget(
                                task: task, storyId: story.id)
                        } label: {
                            HStack(spacing: 3) {
                                Circle()
                                    .fill(taskColor(task.status))
                                    .frame(width: 5, height: 5)
                                Text(task.id)
                                    .font(.caption2.monospaced())
                            }
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(.quaternary.opacity(0.4), in: Capsule())
                        }
                        .buttonStyle(.plain)
                        .help(task.title)
                    }
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func taskColor(_ status: String) -> Color {
        switch status {
        case "done": .green
        case "in_progress": .orange
        case "blocked": .red
        default: .gray
        }
    }
}

// MARK: - the Project view

/// The project's .ai workspace minus the board (which has its own tab):
/// where the project lives on disk and its saved conversations.
struct ProjectView: View {
    @ObservedObject var session: AgentSession
    @EnvironmentObject var store: SessionStore
    var switchToChat: () -> Void

    @State private var board: ScrumBoard?
    @State private var threadList: [ThreadSummary] = []
    @State private var showGitFiles = false
    /// The changed file whose diff is on screen (NEXA-99) — nil = no sheet.
    @State private var diffFile: GitStatus.File? = nil
    private let refresh = Timer.publish(every: 2, on: .main, in: .common).autoconnect()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header
                threadsSection
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onAppear(perform: reload)
        .onReceive(refresh) { _ in reload() }
        .sheet(item: $diffFile) { file in
            DiffView(session: session, file: file)
        }
        // Leaving the sheet drops its pending reply/error so the next open
        // starts from the loading state rather than the last file's diff.
        .onChange(of: diffFile) { target in
            if target == nil {
                session.gitDiff = nil
                session.gitDiffError = nil
            }
        }
    }

    private func reload() {
        board = ScrumBoard.load(projectDir: session.directory)
        threadList = ThreadSummary.loadAll(projectDir: session.directory)
        // Refresh the header's git state on the same tick as everything else
        // (no dedicated poller). A no-op when the process is down.
        session.requestGitStatus()
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(board?.project.isEmpty == false ? board!.project
                 : session.directory.lastPathComponent)
                .font(.title2.bold())
            Text(session.directory.path)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
            // Git working-tree status (NEXA-97): only for repos — non-repo
            // projects render nothing here and stay clean.
            if let git = session.gitStatus {
                gitRow(git)
            }
        }
    }

    // MARK: git status

    /// Branch glyph + name, plus a tappable dirty-count badge when the tree
    /// has changes. The badge opens a popover listing the changed files.
    @ViewBuilder
    private func gitRow(_ git: GitStatus) -> some View {
        HStack(spacing: 8) {
            Label(git.branch, systemImage: "arrow.triangle.branch")
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
            if git.dirtyCount > 0 {
                Button {
                    showGitFiles.toggle()
                } label: {
                    Text("\(git.dirtyCount)")
                        .font(.caption2.monospacedDigit().bold())
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Comb.amber.opacity(0.2), in: Capsule())
                        .foregroundStyle(Comb.amber)
                }
                .buttonStyle(.plain)
                .help("\(git.dirtyCount) changed file\(git.dirtyCount == 1 ? "" : "s")")
                .popover(isPresented: $showGitFiles, arrowEdge: .bottom) {
                    gitFilesPopover(git)
                }
            }
        }
        .padding(.top, 2)
    }

    @ViewBuilder
    private func gitFilesPopover(_ git: GitStatus) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            gitFileSection("Staged", files: git.staged)
            gitFileSection("Unstaged", files: git.unstaged)
            gitFileSection("Untracked", files: git.untracked)
        }
        .padding(14)
        .frame(minWidth: 260, maxWidth: 420, alignment: .leading)
    }

    @ViewBuilder
    private func gitFileSection(_ title: String, files: [GitStatus.File]) -> some View {
        if !files.isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                Text("\(title) (\(files.count))")
                    .font(.caption.bold())
                    .foregroundStyle(Comb.amber)
                ForEach(files) { file in
                    // Tap a file to see its diff (NEXA-99); the row is a
                    // plain button so the popover chrome stays unchanged.
                    Button {
                        showGitFiles = false
                        diffFile = file
                    } label: {
                        Text(file.path)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Show \(file.status == .untracked ? "file" : "diff")")
                }
            }
        }
    }

    // MARK: threads

    @ViewBuilder
    private var threadsSection: some View {
        sectionTitle("Conversations", systemImage: "clock.arrow.circlepath")
        if threadList.isEmpty {
            Text("No saved conversations in this project yet.")
                .font(.callout)
                .foregroundStyle(.secondary)
        } else {
            ForEach(threadList) { thread in
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(thread.title).font(.callout).lineLimit(1)
                        Text(threadSubtitle(thread))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Resume") {
                        session.resumeThread(id: thread.id)
                        switchToChat()
                    }
                    .disabled(!session.isAlive || session.isRunning)
                }
                .padding(.vertical, 2)
            }
        }
    }

    private func threadSubtitle(_ t: ThreadSummary) -> String {
        var parts = ["\(t.messageCount) msgs", t.updated.prefix(19).replacingOccurrences(of: "T", with: " ")]
        parts.append(t.agent + (t.model.map { " · \($0)" } ?? ""))
        return parts.joined(separator: "  ·  ")
    }

    private func sectionTitle(_ title: String, systemImage: String) -> some View {
        Label(title, systemImage: systemImage)
            .font(.headline)
            .padding(.top, 4)
    }
}
