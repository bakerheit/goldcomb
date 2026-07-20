import SwiftUI

/// A Jira/Trello-style kanban board over the project's
/// `.ai/scrum/board.json`: one column per status, cards for every task.
///
/// All edits — creating tickets and moving cards between columns — go through
/// the agent session's `scrum_action` command, which runs the shared scrum
/// engine in the project's folder (so ids, transitions, and the atomic save
/// match the CLI exactly). The view re-reads the board file on the
/// ProjectView timer, so changes made here, by agents, or by other tools all
/// converge.
struct ScrumBoardView: View {
    let board: ScrumBoard
    @ObservedObject var session: AgentSession

    /// Tickets the user is editing right now (optimistic): a dropped card
    /// shows in its new column immediately; the reload cancels the override
    /// once the file catches up. id -> status.
    @State private var moving: [String: String] = [:]
    /// Sheets: creating a story (+first task), adding a task to a story.
    @State private var newTicket: NewTicketTarget? = nil
    @State private var newTaskStory: ScrumStory? = nil
    /// Clicked card: full ticket detail (notes, comments, quick edits).
    @State private var detail: DetailTarget? = nil
    /// Client-side card filter: matches id, title, assignee, story, labels.
    @State private var filter = ""
    /// View mode: "cards" (kanban columns) or "tree" (epic > story > task
    /// hierarchy). Persisted so the choice survives relaunch.
    @AppStorage("scrumBoardViewMode") private var viewMode = "cards"

    static let columns: [(status: String, title: String)] = [
        ("todo", "To do"),
        ("in_progress", "In progress"),
        ("blocked", "Blocked"),
        ("done", "Done"),
    ]

    private struct Column {
        let status: String
        let title: String
        let cards: [ScrumTaskCard]
    }

    /// One card per task anywhere on the board (stories are grouping labels
    /// on the cards, not containers — like JIRA's epic label).
    private struct ScrumTaskCard: Identifiable {
        var task: ScrumTask
        let storyId: String
        let priority: String
        var id: String { task.id }
    }

    struct NewTicketTarget: Identifiable {
        let id = UUID()
        let epic: ScrumEpicGroup?  // nil = no epic
    }

    struct DetailTarget: Identifiable {
        let task: ScrumTask
        let storyId: String
        var id: String { task.id }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            toolbar
            if viewMode == "tree" {
                treeView
            } else {
                HStack(alignment: .top, spacing: 10) {
                    ForEach(columns, id: \.status) { column in
                        columnView(column)
                    }
                }
            }
        }
        .sheet(item: $newTicket) { target in
            NewTicketSheet(epic: target.epic) { title, priority, points in
                createTicket(title: title, priority: priority,
                             points: points, epic: target.epic)
            }
        }
        .sheet(item: $newTaskStory) { story in
            NewTaskSheet(story: story) { title, points in
                session.scrumAction("task_add", fields: [
                    "story": story.id, "title": title, "points": points,
                ])
            }
        }
        .sheet(item: $detail) { target in
            TicketDetailSheet(task: target.task, storyId: target.storyId,
                              session: session)
        }
    }

    // MARK: - toolbar

    private var toolbar: some View {
        HStack(spacing: 10) {
            Menu {
                ForEach(board.groups) { group in
                    Button(group.id == "_none" ? "No epic" : "\(group.id) — \(group.title)") {
                        newTicket = NewTicketTarget(epic: group.id == "_none" ? nil : group)
                    }
                }
                Divider()
                Button("New epic first…") {
                    // A ticket needs a story to live in; with no epics yet we
                    // create one on the fly in the same sheet.
                    newTicket = NewTicketTarget(epic: nil)
                }
            } label: {
                Label("New ticket", systemImage: "plus")
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .disabled(!session.isAlive)
            .help("Create a story with its first task — runs story_add + task_add")

            if !board.groups.isEmpty {
                Menu {
                    ForEach(board.groups.flatMap(\.stories)) { story in
                        Button("\(story.id) — \(story.title)") {
                            newTaskStory = story
                        }
                    }
                } label: {
                    Label("Add task", systemImage: "plus.square.on.square")
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .disabled(!session.isAlive)
                .help("Add a task to an existing story")
            }

            if !session.isAlive {
                Text("start the agent to edit the board")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Picker("View", selection: $viewMode) {
                Text("Cards").tag("cards")
                Text("Tree").tag("tree")
            }
            .pickerStyle(.segmented)
            .fixedSize()
            .labelsHidden()
            .help("Cards: kanban columns. Tree: epic > story > task hierarchy")
            HStack(spacing: 4) {
                Image(systemName: "line.3.horizontal.decrease.circle")
                    .foregroundStyle(.secondary)
                TextField("Filter", text: $filter)
                    .textFieldStyle(.plain)
                    .frame(width: 140)
                if !filter.isEmpty {
                    Button {
                        filter = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.tertiary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(.quaternary.opacity(0.4), in: Capsule())
            .help("Filter cards by ticket id, title, assignee, story, or label")
        }
    }

    private func matchesFilter(_ card: ScrumTaskCard) -> Bool {
        let q = filter.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return true }
        var haystack = [card.id, card.task.title, card.storyId]
        if let who = card.task.assignee { haystack.append("@" + who); haystack.append(who) }
        haystack.append(contentsOf: card.task.labels)
        return haystack.contains { $0.lowercased().contains(q) }
    }

    // MARK: - tree

    /// Task rows for the tree, after the filter (and any optimistic moves).
    /// A story or epic stays visible while any task under it matches.
    private func treeTasks(_ story: ScrumStory) -> [ScrumTask] {
        let filtered = story.tasks.filter {
            matchesFilter(ScrumTaskCard(task: $0, storyId: story.id,
                                        priority: story.priority))
        }
        // Done tasks: most recently finished first, like the kanban lane.
        return filtered.sorted {
            if $0.status == "done", $1.status == "done" {
                return Self.doneSortKey($0) > Self.doneSortKey($1)
            }
            return false  // keep board order otherwise
        }
    }

    private func points(_ tasks: [ScrumTask]) -> (done: Int, total: Int) {
        (tasks.filter { $0.status == "done" }.reduce(0) { $0 + $1.points },
         tasks.reduce(0) { $0 + $1.points })
    }

    private var treeView: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(board.groups) { group in
                    let stories = group.stories.filter { !treeTasks($0).isEmpty }
                    if !stories.isEmpty {
                        treeEpicRow(group, stories: stories)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(4)
        }
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
    }

    /// Epic: id + title, rolled-up story points (done/total), story count.
    private func treeEpicRow(_ group: ScrumEpicGroup,
                             stories: [ScrumStory]) -> some View {
        let tasks = stories.flatMap(\.tasks)
        let pts = points(tasks)
        return DisclosureGroup {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(stories) { story in
                    treeStoryRow(story)
                }
            }
            .padding(.leading, 16)
        } label: {
            HStack(spacing: 8) {
                Text(group.id)
                    .font(.callout.monospaced().bold())
                    .foregroundStyle(.tint)
                Text(group.title)
                    .font(.callout.weight(.semibold))
                Spacer(minLength: 0)
                Text("\(stories.count) \(stories.count == 1 ? "story" : "stories")")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if pts.total > 0 {
                    Text("\(pts.done)/\(pts.total)pt")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(8)
        .background(.background, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary, lineWidth: 1))
    }

    /// Story: id + title + priority, task done/total, plus an add-task
    /// affordance (uses the same sheet as the toolbar menu).
    private func treeStoryRow(_ story: ScrumStory) -> some View {
        let tasks = treeTasks(story)
        let done = tasks.filter { $0.status == "done" }.count
        return DisclosureGroup {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(tasks) { task in
                    treeTaskRow(task, storyId: story.id)
                }
            }
            .padding(.leading, 16)
        } label: {
            HStack(spacing: 8) {
                Text(story.id)
                    .font(.caption.monospaced().bold())
                    .foregroundStyle(.secondary)
                Text(story.title)
                    .font(.callout)
                if story.priority == "high" {
                    Image(systemName: "exclamationmark")
                        .font(.caption2.bold())
                        .foregroundStyle(.red)
                        .help("High priority story")
                } else if story.priority == "low" {
                    Text("low")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                Text("\(done)/\(tasks.count) tasks")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                Button {
                    newTaskStory = story
                } label: {
                    Image(systemName: "plus.circle")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.tint)
                .disabled(!session.isAlive)
                .help("Add a task to \(story.id)")
            }
        }
        .padding(6)
        .background(.background, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary, lineWidth: 1))
    }

    /// Task: id + title, status dot, assignee, points. Click opens the
    /// same detail sheet as a kanban card.
    private func treeTaskRow(_ task: ScrumTask, storyId: String) -> some View {
        HStack(spacing: 8) {
            Circle().fill(columnColor(task.status)).frame(width: 7, height: 7)
            Text(task.id)
                .font(.caption2.monospaced().bold())
                .foregroundStyle(columnColor(task.status))
            Text(task.title)
                .font(.callout)
                .lineLimit(1)
            if let who = task.assignee {
                Text("@\(who)")
                    .font(.caption2)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(.tint.opacity(0.12), in: Capsule())
            }
            Spacer(minLength: 0)
            if task.points > 0 {
                Text("\(task.points)pt")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 8).padding(.vertical, 5)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background, in: RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary, lineWidth: 1))
        .contentShape(Rectangle())
        .onTapGesture {
            detail = DetailTarget(task: task, storyId: storyId)
        }
    }

    // MARK: - columns

    /// Cards per status, with any optimistic moves applied. A move is cleared
    /// as soon as the file reflects it (or if the task vanished).
    private var columns: [Column] {
        let cards = board.groups.flatMap { group in
            group.stories.flatMap { story in
                story.tasks.map {
                    ScrumTaskCard(task: $0, storyId: story.id, priority: story.priority)
                }
            }
        }
        var effective: [ScrumTaskCard] = []
        for card in cards {
            if let want = moving[card.id] {
                if card.task.status == want {
                    moving.removeValue(forKey: card.id)  // file caught up
                } else {
                    var c = card
                    c.task.status = want
                    effective.append(c)
                    continue
                }
            }
            effective.append(card)
        }
        return Self.columns.map { col in
            let cards = effective.filter {
                $0.task.status == col.status && matchesFilter($0)
            }
            // Done lane: most recently finished first. Older boards have no
            // done_at — those cards keep their relative board order (the
            // doneAt ?? created ?? 0 key ties at 0 and the sort is stable).
            let ordered = col.status == "done"
                ? cards.sorted { Self.doneSortKey($0.task) > Self.doneSortKey($1.task) }
                : cards
            return Column(status: col.status, title: col.title, cards: ordered)
        }
    }

    /// Done-lane ordering key: when the task finished, else when it was
    /// created, else 0 (undated tasks sort last, stably).
    static func doneSortKey(_ task: ScrumTask) -> Double {
        task.doneAt ?? task.created ?? 0
    }

    private func columnView(_ column: Column) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Circle().fill(columnColor(column.status)).frame(width: 7, height: 7)
                Text(column.title).font(.caption.weight(.semibold))
                Text("\(column.cards.count)")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 4)
            ForEach(column.cards) { card in
                cardView(card)
                    .onDrag {
                        NSItemProvider(object: card.id as NSString)
                    }
                    .onTapGesture {
                        detail = DetailTarget(task: card.task, storyId: card.storyId)
                    }
            }
            if column.cards.isEmpty {
                Text("—")
                    .font(.caption)
                    .foregroundStyle(.quaternary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
            }
            Spacer(minLength: 0)
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
        .onDrop(of: [.text], isTargeted: nil) { providers in
            guard let provider = providers.first else { return false }
            provider.loadObject(ofClass: NSString.self) { object, _ in
                guard let id = object as? String else { return }
                DispatchQueue.main.async { move(taskId: id, to: column.status) }
            }
            return true
        }
    }

    // MARK: - cards

    private func cardView(_ card: ScrumTaskCard) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Text(card.id)
                    .font(.caption2.monospaced().bold())
                    .foregroundStyle(columnColor(card.task.status))
                Spacer(minLength: 0)
                if card.priority == "high" {
                    Image(systemName: "exclamationmark")
                        .font(.caption2.bold())
                        .foregroundStyle(.red)
                        .help("High priority story")
                }
                if let who = card.task.assignee {
                    Text("@\(who)")
                        .font(.caption2)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(.tint.opacity(0.12), in: Capsule())
                }
            }
            Text(card.task.title)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
            if !card.task.labels.isEmpty {
                HStack(spacing: 4) {
                    ForEach(card.task.labels, id: \.self) { label in
                        Text(label)
                            .font(.caption2)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Comb.gold.opacity(0.15), in: Capsule())
                            .foregroundStyle(Comb.amber)
                    }
                }
            }
            HStack(spacing: 6) {
                Text(card.storyId)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                if card.task.points > 0 {
                    Text("\(card.task.points)pt")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                if board.sprint?.active == true,
                   board.sprint?.storyIds.contains(card.storyId) == true {
                    Image(systemName: "flag.fill")
                        .font(.caption2)
                        .foregroundStyle(.green)
                        .help("In the active sprint")
                }
                if !card.task.blockedBy.isEmpty {
                    Label("\(card.task.blockedBy.count)", systemImage: "lock.fill")
                        .font(.caption2)
                        .foregroundStyle(.red)
                        .labelStyle(.titleAndIcon)
                        .help("Blocked by " + card.task.blockedBy.joined(separator: ", "))
                }
                if card.task.commentCount > 0 {
                    Label("\(card.task.commentCount)", systemImage: "text.bubble")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .labelStyle(.titleAndIcon)
                }
                Spacer(minLength: 0)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary, lineWidth: 1))
        .shadow(color: .black.opacity(0.06), radius: 1, y: 1)
        .contextMenu {
            ForEach(Self.columns.filter { $0.status != card.task.status }, id: \.status) { col in
                Button("Move to \(col.title)") { move(taskId: card.id, to: col.status) }
            }
            Divider()
            if card.task.assignee != session.name {
                Button("Assign to @\(session.name)") {
                    session.scrumAction("assign", fields: [
                        "ticket": card.id, "assignee": session.name,
                    ])
                }
            }
            if card.task.assignee != nil {
                Button("Unassign") {
                    session.scrumAction("task_update", fields: [
                        "task": card.id, "assignee": "",
                    ])
                }
            }
            if let sprint = board.sprint, sprint.active {
                Divider()
                if sprint.storyIds.contains(card.storyId) {
                    Button("Remove story from sprint") {
                        session.scrumAction("sprint_remove", fields: ["story": card.storyId])
                    }
                } else {
                    Button("Add story to sprint \(sprint.number)") {
                        session.scrumAction("sprint_add", fields: ["story": card.storyId])
                    }
                }
            }
            Divider()
            Button("Delete task", role: .destructive) {
                session.scrumAction("task_del", fields: ["task": card.id])
            }
        }
    }

    // MARK: - edits

    private func move(taskId: String, to status: String) {
        guard session.isAlive else { return }
        moving[taskId] = status
        session.scrumAction("task_update", fields: ["task": taskId, "status": status])
    }

    private func createTicket(title: String, priority: String, points: Int,
                              epic: ScrumEpicGroup?) {
        session.createTicket(title: title, priority: priority, points: points,
                             epic: epic)
    }

    private func columnColor(_ status: String) -> Color {
        switch status {
        case "done": .green
        case "in_progress": .orange
        case "blocked": .red
        default: .gray
        }
    }
}

// MARK: - shared ticket creation

extension AgentSession {
    /// One atomic engine call: ticket_add makes the story + first task (and an
    /// epic when none exists). Omitting `sprint` keeps it in the backlog.
    func createTicket(title: String, priority: String, points: Int,
                      epic: ScrumEpicGroup?) {
        guard isAlive else { return }
        var fields: [String: Any] = ["title": title, "priority": priority,
                                     "points": points]
        if let epic { fields["epic"] = epic.id }
        scrumAction("ticket_add", fields: fields)
    }
}

// MARK: - sheets

/// Full ticket detail for a clicked card: status, labels, notes, and the
/// comment thread — the parts a kanban card can only hint at. Edits fire
/// scrum actions; the board file refresh brings the truth back.
struct TicketDetailSheet: View {
    let task: ScrumTask
    let storyId: String
    @ObservedObject var session: AgentSession
    @Environment(\.dismiss) private var dismiss

    @State private var newComment = ""
    /// Optimistic echo of comments sent from this sheet (the snapshot in
    /// `task` doesn't update while the sheet is open).
    @State private var sent: [String] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                Text(task.id).font(.title3.monospaced().bold())
                Text(task.title).font(.title3)
                Spacer()
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
            }

            HStack(spacing: 12) {
                Menu {
                    ForEach(ScrumBoardView.columns, id: \.status) { col in
                        Button(col.title) {
                            session.scrumAction("task_update", fields: [
                                "task": task.id, "status": col.status,
                            ])
                            dismiss()
                        }
                        .disabled(col.status == task.status)
                    }
                } label: {
                    Text(statusTitle(task.status))
                }
                .fixedSize()
                .disabled(!session.isAlive)
                Text(storyId).font(.callout.monospaced()).foregroundStyle(.secondary)
                if task.points > 0 {
                    Text("\(task.points)pt").foregroundStyle(.secondary)
                }
                if let who = task.assignee {
                    Text("@\(who)")
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(.tint.opacity(0.12), in: Capsule())
                }
                Spacer()
            }
            .font(.callout)

            if !task.labels.isEmpty {
                HStack(spacing: 4) {
                    ForEach(task.labels, id: \.self) { label in
                        Text(label)
                            .font(.caption)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Comb.gold.opacity(0.15), in: Capsule())
                            .foregroundStyle(Comb.amber)
                    }
                }
            }
            if !task.blockedBy.isEmpty {
                Label("Blocked by \(task.blockedBy.joined(separator: ", "))",
                      systemImage: "lock.fill")
                    .font(.callout)
                    .foregroundStyle(.red)
            }
            if !task.notes.isEmpty {
                Text(task.notes)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.quaternary.opacity(0.3),
                                in: RoundedRectangle(cornerRadius: 8))
            }

            Divider()
            Text("Comments").font(.headline)
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    if task.comments.isEmpty && sent.isEmpty {
                        Text("No comments yet.")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    ForEach(task.comments) { c in
                        commentRow(who: c.who, when: Self.stamp(c.at), text: c.text)
                    }
                    ForEach(sent, id: \.self) { text in
                        commentRow(who: "you", when: "sending…", text: text)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(minHeight: 60, maxHeight: 180)

            HStack {
                TextField("Add a comment…", text: $newComment)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(sendComment)
                Button("Send", action: sendComment)
                    .disabled(!session.isAlive ||
                              newComment.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(20)
        .frame(width: 480)
    }

    private func sendComment() {
        let text = newComment.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty, session.isAlive else { return }
        session.scrumAction("comment", fields: ["ticket": task.id, "text": text])
        sent.append(text)
        newComment = ""
    }

    private func commentRow(who: String, when: String, text: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text("@\(who)").font(.caption.bold())
                Text(when).font(.caption2).foregroundStyle(.secondary)
            }
            Text(text).font(.callout)
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 8))
    }

    private func statusTitle(_ status: String) -> String {
        ScrumBoardView.columns.first { $0.status == status }?.title ?? status
    }

    private static func stamp(_ at: Double) -> String {
        guard at > 0 else { return "" }
        let fmt = DateFormatter()
        fmt.dateFormat = "MMM d HH:mm"
        return fmt.string(from: Date(timeIntervalSince1970: at))
    }
}

/// New user-facing ticket: a story plus its first task (the board cards are
/// tasks, so a story without one would be invisible).
struct NewTicketSheet: View {
    let epic: ScrumEpicGroup?
    var onCreate: (String, String, Int) -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var title = ""
    @State private var priority = "medium"
    @State private var points = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(epic.map { "New ticket in \($0.title)" } ?? "New ticket")
                .font(.title3.bold())
            TextField("What needs doing?", text: $title)
            Picker("Priority", selection: $priority) {
                Text("Low").tag("low")
                Text("Medium").tag("medium")
                Text("High").tag("high")
            }
            .pickerStyle(.segmented)
            Stepper("Points: \(points)", value: $points, in: 0...13)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Create") {
                    let t = title.trimmingCharacters(in: .whitespaces)
                    guard !t.isEmpty else { return }
                    onCreate(t, priority, points)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(20)
        .frame(width: 400)
    }
}

private struct NewTaskSheet: View {
    let story: ScrumStory
    var onCreate: (String, Int) -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var title = ""
    @State private var points = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("New task in \(story.id)").font(.title3.bold())
            Text(story.title).font(.callout).foregroundStyle(.secondary)
            TextField("Task title", text: $title)
            Stepper("Points: \(points)", value: $points, in: 0...13)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Add") {
                    let t = title.trimmingCharacters(in: .whitespaces)
                    guard !t.isEmpty else { return }
                    onCreate(t, points)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(20)
        .frame(width: 400)
    }
}
