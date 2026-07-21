import SwiftUI

extension Notification.Name {
    /// Posted by a project header's context menu; `object` is the Project.
    static let newAgentRequested = Notification.Name("newAgentRequested")
    static let renameProjectRequested = Notification.Name("renameProjectRequested")
    static let removeProjectRequested = Notification.Name("removeProjectRequested")
}

/// Identifiable wrapper for the new-agent sheet: `project` nil means the
/// agent is created ungrouped (it picks its own folder).
struct AgentSheetTarget: Identifiable {
    let id = UUID()
    let project: Project?
}

struct ContentView: View {
    @EnvironmentObject var store: SessionStore
    @State private var showNewProject = false
    @State private var agentTarget: AgentSheetTarget? = nil
    @State private var renaming: Project? = nil
    @State private var draftName = ""
    @State private var removing: Project? = nil

    var body: some View {
        NavigationSplitView {
            sidebarList
        } detail: {
            detailPane
        }
        .sheet(isPresented: $showNewProject) {
            NewProjectSheet()
        }
        .sheet(item: $agentTarget) { target in
            NewSessionSheet(project: target.project)
        }
        .alert("Rename project", isPresented: renameAlert) {
            TextField("Project name", text: $draftName)
            Button("Rename") {
                let n = draftName.trimmingCharacters(in: .whitespaces)
                if !n.isEmpty, let project = renaming {
                    store.rename(project, to: n)
                }
            }
            Button("Cancel", role: .cancel) {}
        }
        .alert(
            "Remove “\(removing?.name ?? "")”?",
            isPresented: removeAlert,
            presenting: removing
        ) { project in
            Button("Remove", role: .destructive) {
                store.removeProject(project)
            }
            Button("Cancel", role: .cancel) {}
        } message: { project in
            let n = store.sessionsFor(project).count
            Text("The project and its \(n) \(n == 1 ? "agent" : "agents") will be removed from the app. Nothing is deleted from \(project.directory.path).")
        }
        .onReceive(NotificationCenter.default.publisher(for: .newAgentRequested)) { note in
            if let project = note.object as? Project {
                agentTarget = AgentSheetTarget(project: project)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .renameProjectRequested)) { note in
            if let project = note.object as? Project {
                draftName = project.name
                renaming = project
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .removeProjectRequested)) { note in
            if let project = note.object as? Project {
                removing = project
            }
        }
        .onDisappear { store.shutdownAll() }
    }

    private var renameAlert: Binding<Bool> {
        Binding(
            get: { renaming != nil },
            set: { if !$0 { renaming = nil } }
        )
    }

    private var removeAlert: Binding<Bool> {
        Binding(
            get: { removing != nil },
            set: { if !$0 { removing = nil } }
        )
    }

    // MARK: sidebar

    private var sidebarList: some View {
        List(selection: $store.selection) {
            // Snapshot of every live field the rows render. The data
            // dependency MUST live in the List's own body: live state is
            // otherwise read only inside child views (ProjectCard,
            // AgentSidebarRow), and a nested @ObservedObject read invalidates
            // the child but does not re-diff NSTableView rows until user
            // interaction. Subagent statuses are included because subagent_end
            // mutates them in place (no .count change); chat rooms because
            // unread badges / paused markers change without a row count
            // change (NEXA-71, same re-diff path as NEXA-43/44). See
            // SubAgentRows.records and store.chatReadTick.
            let pulse = store.sessions.map { s in
                "\(s.id.uuidString)|\(s.isRunning)|\(s.isAlive)|\(s.pendingConfirm != nil)|\(s.pendingQuestions != nil)|\(s.status ?? "")|\(s.sudo)|\(s.provider)|\(s.model)|"
                    + s.subagents.map { "\($0.id):\($0.status)" }.joined(separator: ",")
            }
            let chatPulse = "\(store.chatReadTick)|" + store.chats.values.flatMap { $0 }
                .map { "\($0.id):\($0.messages.count):\($0.isPaused)" }
                .joined(separator: ",")
            let _ = (pulse, chatPulse)
            SidebarBrand()
                .listRowSeparator(.hidden)
                .selectionDisabled()
            ForEach(store.projects) { project in
                ProjectCard(project: project)
                    .tag(SidebarItem.project(project.id))
                    .listRowSeparator(.hidden)
                if !store.collapsed.contains(project.id) {
                    agentRows(store.sessionsFor(project),
                              tint: Comb.tint(for: project.name),
                              records: store.subAgents[project.id] ?? [])
                    // Deploy records that haven't been promoted into a real
                    // agent row yet (promotion normally happens within a poll
                    // tick of deploy start).
                    SubAgentRows(project: project,
                                 records: store.subAgents[project.id] ?? [])
                    chatRows(for: project)
                }
            }
            let ungrouped = store.sessions.filter { $0.projectID == nil }
            if !ungrouped.isEmpty {
                Text(store.projects.isEmpty ? "AGENTS" : "UNGROUPED")
                    .font(.system(size: 10, weight: .semibold))
                    .kerning(0.8)
                    .foregroundStyle(.tertiary)
                    .padding(.top, 6)
                    .listRowSeparator(.hidden)
                    .selectionDisabled()
                agentRows(ungrouped, tint: .gray)
            }
        }
        .listStyle(.sidebar)
        .navigationSplitViewColumnWidth(min: 220, ideal: 252)
        .safeAreaInset(edge: .bottom, spacing: 0) { SidebarFooter() }
        .toolbar {
            ToolbarItem {
                Menu {
                    Button("New project…") { showNewProject = true }
                    if !store.projects.isEmpty {
                        Menu("New agent in") {
                            ForEach(store.projects) { project in
                                Button(project.name) {
                                    agentTarget = AgentSheetTarget(project: project)
                                }
                            }
                        }
                    }
                    Button("New agent") { agentTarget = AgentSheetTarget(project: nil) }
                } label: {
                    Label("New", systemImage: "plus")
                }
            }
        }
        .overlay {
            if store.projects.isEmpty && store.sessions.isEmpty {
                ContentUnavailableView(
                    "No projects",
                    systemImage: "folder.badge.plus",
                    description: Text("Add a project, then create agents inside it.")
                )
            }
        }
    }

    /// The project's chat rooms, under a small header. Rows carry a `.chat`
    /// tag so selecting one routes the detail pane to ChatRoomView.
    @ViewBuilder
    private func chatRows(for project: Project) -> some View {
        let rooms = store.chats[project.id] ?? []
        if !rooms.isEmpty {
            Text("CHATS")
                .font(.system(size: 9, weight: .semibold))
                .kerning(0.8)
                .foregroundStyle(.tertiary)
                .padding(.leading, 10)
                .padding(.top, 4)
                .listRowSeparator(.hidden)
                .selectionDisabled()
            ForEach(rooms) { room in
                ChatSidebarRow(room: room, tint: Comb.tint(for: project.name))
                    .tag(SidebarItem.chat(room.id))
                    .listRowSeparator(.hidden)
            }
        }
    }

    // MARK: detail

    @ViewBuilder
    private var detailPane: some View {
        switch store.selection {
        case .agent(let id):
            agentDetail(id)
        case .chat(let roomID):
            if let room = store.room(withID: roomID) {
                ChatRoomView(room: room).id(room.id)
            } else {
                unavailableDetail
            }
        case .project(let id):
            projectDetail(id)
        case nil:
            unavailableDetail
        }
    }

    @ViewBuilder
    private func agentDetail(_ id: UUID) -> some View {
        if let session = store.sessions.first(where: { $0.id == id }) {
            HStack(spacing: 0) {
                ChatView(session: session)
                    .id(session.id)
                    .frame(maxWidth: .infinity)
                if store.explorerVisible, let explorer = store.activeExplorer {
                    Divider()
                    FileExplorerView(model: explorer)
                        .frame(minWidth: 200, idealWidth: 240, maxWidth: 320)
                        .onChange(of: session.isRunning) { _, running in
                            if !running { explorer.refresh() }
                        }
                }
            }
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        store.explorerVisible.toggle()
                    } label: {
                        Label("Files", systemImage: "sidebar.right")
                    }
                    .help("Show or hide the project file explorer")
                }
            }
        } else {
            unavailableDetail
        }
    }

    @ViewBuilder
    private func projectDetail(_ id: UUID) -> some View {
        if let project = store.projects.first(where: { $0.id == id }) {
            HStack(spacing: 0) {
                ProjectDetailView(project: project)
                    .frame(maxWidth: .infinity)
                if store.explorerVisible, let explorer = store.activeExplorer {
                    Divider()
                    FileExplorerView(model: explorer)
                        .frame(minWidth: 200, idealWidth: 240, maxWidth: 320)
                        .onChange(of: store.actionSession(for: project)?.isRunning ?? false) { _, running in
                            if !running { explorer.refresh() }
                        }
                }
            }
        } else {
            unavailableDetail
        }
    }

    @ViewBuilder
    private func agentRows(_ sessions: [AgentSession], tint: Color,
                           records: [SubAgentRecord] = []) -> some View {
        ForEach(sessions) { session in
            AgentSidebarRow(session: session, tint: tint,
                            busyDeploy: records.first {
                                $0.isLive && $0.label == session.name
                            })
                .tag(SidebarItem.agent(session.id))
                .listRowSeparator(.hidden)
                .contextMenu {
                    Button("Interrupt") { session.interrupt() }
                    Button("Close agent", role: .destructive) {
                        store.remove(session)
                    }
                }
            // Sub-agents are transient live-event state: untagged rows under
            // their parent agent, so they can never become the selection.
            // Once a deploy is promoted to a real roster row, that row (blue,
            // busy) is the single surface — skip the duplicate here.
            ForEach(session.subagents.filter { sub in
                !sessions.contains { $0.name == sub.label }
            }) { subagent in
                SubagentSidebarRow(subagent: subagent, tint: tint)
                    .listRowSeparator(.hidden)
                    .selectionDisabled()
            }
        }
    }
}

extension ContentView {
    /// The detail pane shown when nothing (valid) is selected.
    private var unavailableDetail: some View {
    ContentUnavailableView(
        "Select an agent",
        systemImage: "bubble.left.and.bubble.right",
        description: Text("Add a project, then create agents inside it; they run in parallel.")
    )
    }
}

/// A selected project's home: its .ai workspace, team chart, and ticket
/// board. Board/thread actions run through the project's most recent live
/// agent; with no agents yet, Agents shows its empty state with Add working.
struct ProjectDetailView: View {
    @EnvironmentObject var store: SessionStore
    let project: Project
    @State private var tab: Tab = .project

    enum Tab: String, CaseIterable {
        case project = "Project"
        case agents = "Agents"
        case chats = "Chats"
        case sprint = "Sprint"
        case backlog = "Backlog"
    }

    private var actionSession: AgentSession? { store.actionSession(for: project) }

    private var chatUnread: Int {
        (store.chats[project.id] ?? [])
            .reduce(0) { $0 + ChatReadState.unread($1) }
    }

    var body: some View {
        Group {
            if let session = actionSession {
                switch tab {
                case .project:
                    ProjectView(session: session) {
                        store.selection = .agent(session.id)
                    }
                case .agents:
                    AgentsTabView(session: session)
                case .chats:
                    ChatsTabView(project: project)
                case .sprint:
                    SprintTabView(session: session)
                case .backlog:
                    BacklogTabView(session: session)
                }
            } else {
                ContentUnavailableView(
                    "No agents in \(project.name)",
                    systemImage: "person.badge.plus",
                    description: Text("Create an agent in this project to use its workspace, team, and board.")
                )
            }
        }
        .toolbar {
            ToolbarItem(placement: .navigation) {
                Picker("View", selection: $tab) {
                    ForEach(Tab.allCases, id: \.self) { t in
                        // A dot marks unread chat traffic; segmented items
                        // can't carry a real badge.
                        Text(t == .chats && chatUnread > 0
                             ? "\(t.rawValue) ●" : t.rawValue)
                            .tag(t)
                    }
                }
                .pickerStyle(.segmented)
                .frame(width: 460)
            }
            ToolbarItem(placement: .primaryAction) {
                Button {
                    store.explorerVisible.toggle()
                } label: {
                    Label("Files", systemImage: "sidebar.right")
                }
                .help("Show or hide the project file explorer")
                // No agent means no directory to explore.
                .disabled(actionSession == nil)
            }
        }
        // New tab content when switching projects.
        .id(project.id)
        // A chat ticket-link jump (NEXA-84) lands here: open the Sprint board
        // so the ticket is in view. onAppear covers the fresh mount when the
        // link also changed the selection to this project; onChange covers a
        // jump while this project is already showing. One-shot: clear it.
        .onAppear { consumeTicketFocus() }
        .onChange(of: store.pendingTicketFocus) { _, id in
            if id != nil { consumeTicketFocus() }
        }
    }

    private func consumeTicketFocus() {
        guard store.pendingTicketFocus != nil else { return }
        tab = .sprint
        store.pendingTicketFocus = nil
    }
}

struct NewProjectSheet: View {
    @EnvironmentObject var store: SessionStore
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var directory =
        FileManager.default.homeDirectoryForCurrentUser
    @State private var showPicker = false
    @State private var withAgent = true

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("New project").font(.title3.bold())
            TextField("Name (defaults to the folder name)", text: $name)
            HStack {
                Text("Folder:")
                Text(directory.path)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer()
                Button("Choose…") { showPicker = true }
            }
            Toggle("Create an agent in this project", isOn: $withAgent)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Add project") {
                    let project = store.createProject(name: name, directory: directory)
                    if withAgent {
                        store.createSession(
                            name: "", directory: project.directory, sudo: false,
                            in: project
                        )
                    }
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 440)
        .fileImporter(
            isPresented: $showPicker,
            allowedContentTypes: [.folder]
        ) { result in
            if case .success(let url) = result {
                directory = url
            }
        }
    }
}

struct NewSessionSheet: View {
    @EnvironmentObject var store: SessionStore
    @Environment(\.dismiss) private var dismiss

    /// When set, the agent joins this project and runs in its folder.
    let project: Project?

    @State private var name = Names.random()
    @State private var directory =
        FileManager.default.homeDirectoryForCurrentUser
    @State private var role = ""
    @State private var agentDescription = ""
    @State private var sudo = false
    @State private var showPicker = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(project.map { "New agent in \($0.name)" } ?? "New agent")
                .font(.title3.bold())
            HStack {
                TextField("Name", text: $name)
                Button {
                    name = Names.random()
                } label: {
                    Image(systemName: "dice")
                }
                .help("Roll a different name")
            }
            TextField("Role (e.g. Backend engineer)", text: $role)
            TextField("Description (optional)", text: $agentDescription, axis: .vertical)
                .lineLimit(2...4)
            if let project {
                LabeledContent("Folder:") {
                    Text(project.directory.path)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            } else {
                HStack {
                    Text("Folder:")
                    Text(directory.path)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer()
                    Button("Choose…") { showPicker = true }
                }
            }
            Toggle("sudo — run tool calls without asking", isOn: $sudo)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Create") {
                    store.createSession(
                        name: name,
                        directory: project?.directory ?? directory,
                        sudo: sudo,
                        role: role,
                        description: agentDescription,
                        in: project
                    )
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 440)
        .fileImporter(
            isPresented: $showPicker,
            allowedContentTypes: [.folder]
        ) { result in
            if case .success(let url) = result {
                directory = url
            }
        }
    }
}
