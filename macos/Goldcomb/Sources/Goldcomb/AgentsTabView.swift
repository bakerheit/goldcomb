import SwiftUI

/// The project's agent team as an org chart: node cards connected by elbow
/// lines, roots on top, reports fanning out beneath their lead.
///
/// Structure is functional, not decorative — every agent launches with a
/// `--team` system-prompt block naming its lead, teammates, and reports, so
/// delegation over the ticket board works by name. The tree persists in
/// SidebarState.json; edits reach an agent's team snapshot when it next
/// (re)starts.
struct AgentsTabView: View {
    @ObservedObject var session: AgentSession
    @EnvironmentObject var store: SessionStore

    @State private var addTarget: AddTarget? = nil
    @State private var removing: AgentSession? = nil
    /// The card currently hovered by a dragged agent (gold highlight).
    @State private var dropTargetID: UUID? = nil

    struct AddTarget: Identifiable {
        let id = UUID()
        let parent: AgentSession?   // nil = add a root agent
    }

    /// Card-bounds anchors keyed by agent id, collected up the tree so the
    /// background pass can draw parent -> child connectors.
    private struct NodeAnchorKey: PreferenceKey {
        static var defaultValue: [UUID: Anchor<CGRect>] = [:]
        static func reduce(value: inout [UUID: Anchor<CGRect>],
                           nextValue: () -> [UUID: Anchor<CGRect>]) {
            value.merge(nextValue()) { $1 }
        }
    }

    /// Agents belonging to this project (or, for ungrouped agents, sharing
    /// the same folder).
    private var scoped: [AgentSession] {
        store.sessions.filter { candidate in
            session.projectID != nil
                ? candidate.projectID == session.projectID
                : candidate.directory == session.directory
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Team", systemImage: "point.3.filled.connected.trianglepath.dotted")
                    .font(.headline)
                Spacer()
                Button {
                    addTarget = AddTarget(parent: nil)
                } label: {
                    Label("Add agent", systemImage: "plus")
                }
            }
            .padding([.top, .horizontal], 16)
            Text("Each agent knows its lead, teammates, and reports — they "
                 + "coordinate by assigning tickets and commenting on the "
                 + "board. Drag a card onto a new lead (or onto empty space "
                 + "to make it a root); changes reach an agent when it next "
                 + "starts.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 16)

            let roots = store.treeRoots(among: scoped)
            if roots.isEmpty {
                ContentUnavailableView(
                    "No agents yet",
                    systemImage: "person.3",
                    description: Text("Add a root agent — a planner makes a "
                                      + "good tree root — then grow the team "
                                      + "under it.")
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView([.horizontal, .vertical]) {
                    HStack(alignment: .top, spacing: 48) {
                        ForEach(roots) { root in
                            subtree(root)
                        }
                    }
                    .padding(32)
                    .backgroundPreferenceValue(NodeAnchorKey.self) { anchors in
                        GeometryReader { geo in
                            connectors(in: geo, anchors: anchors)
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .onDrop(of: [.text], isTargeted: nil) { providers in
                    handleDrop(providers, onto: nil)   // empty space = root
                }
            }
        }
        .sheet(item: $addTarget) { target in
            AddTreeAgentSheet(parent: target.parent) {
                name, personaRole, role, description, sudo in
                let project = store.projects.first { $0.id == session.projectID }
                store.createSession(
                    name: name,
                    directory: project?.directory ?? session.directory,
                    sudo: sudo,
                    personaRole: personaRole,
                    role: role,
                    description: description,
                    parentID: target.parent?.id,
                    in: project
                )
            }
        }
        .confirmationDialog(
            "Remove “\(removing?.name ?? "")” from the team?",
            isPresented: Binding(
                get: { removing != nil },
                set: { if !$0 { removing = nil } }
            ),
            presenting: removing
        ) { agent in
            Button("Remove agent", role: .destructive) {
                store.removeFromTree(agent)
            }
            Button("Cancel", role: .cancel) {}
        } message: { agent in
            let n = store.children(of: agent.id).count
            Text(n > 0
                 ? "Its \(n) report\(n == 1 ? "" : "s") move up to its lead. "
                   + "The agent process stops; no files are deleted."
                 : "The agent process stops; no files are deleted.")
        }
    }

    /// Resolve a dragged agent id and reparent it under `target`
    /// (nil = make it a root). Cycle/self checks live in the store.
    private func handleDrop(_ providers: [NSItemProvider],
                            onto target: AgentSession?) -> Bool {
        guard let provider = providers.first else { return false }
        provider.loadObject(ofClass: NSString.self) { object, _ in
            guard let raw = object as? String, let id = UUID(uuidString: raw)
            else { return }
            DispatchQueue.main.async {
                guard let dragged = store.sessions.first(where: { $0.id == id })
                else { return }
                store.reparent(dragged, under: target)
                dropTargetID = nil
            }
        }
        return true
    }

    // MARK: chart

    /// One subtree: this agent's card, then its reports fanned out beneath.
    private func subtree(_ agent: AgentSession) -> AnyView {
        let kids = store.children(of: agent.id)
        return AnyView(
            VStack(spacing: 36) {
                AgentNodeCard(
                    agent: agent,
                    isCurrent: agent.id == session.id,
                    isDropTarget: dropTargetID == agent.id,
                    onChat: { store.selection = .agent(agent.id) },
                    onAddChild: { addTarget = AddTarget(parent: agent) },
                    onRemove: { removing = agent }
                )
                .onDrag {
                    NSItemProvider(object: agent.id.uuidString as NSString)
                }
                .onDrop(of: [.text], isTargeted: Binding(
                    get: { dropTargetID == agent.id },
                    set: { dropTargetID = $0 ? agent.id : nil }
                )) { providers in
                    handleDrop(providers, onto: agent)
                }
                .anchorPreference(key: NodeAnchorKey.self, value: .bounds) {
                    [agent.id: $0]
                }
                if !kids.isEmpty {
                    HStack(alignment: .top, spacing: 24) {
                        ForEach(kids) { child in
                            subtree(child)
                        }
                    }
                }
            }
        )
    }

    /// Elbow connectors: parent bottom-center down to a midline, across, and
    /// down into each report's top-center.
    private func connectors(in geo: GeometryProxy,
                            anchors: [UUID: Anchor<CGRect>]) -> some View {
        Path { path in
            for parent in scoped {
                guard let pa = anchors[parent.id] else { continue }
                let pr = geo[pa]
                for child in store.children(of: parent.id) {
                    guard let ca = anchors[child.id] else { continue }
                    let cr = geo[ca]
                    let start = CGPoint(x: pr.midX, y: pr.maxY)
                    let end = CGPoint(x: cr.midX, y: cr.minY)
                    let midY = (start.y + end.y) / 2
                    path.move(to: start)
                    path.addLine(to: CGPoint(x: start.x, y: midY))
                    path.addLine(to: CGPoint(x: end.x, y: midY))
                    path.addLine(to: end)
                }
            }
        }
        .stroke(Comb.gold.opacity(0.5),
                style: StrokeStyle(lineWidth: 1.5, lineCap: .round,
                                   lineJoin: .round))
    }
}

/// One box on the chart: status, human name, role, and the card actions.
private struct AgentNodeCard: View {
    @ObservedObject var agent: AgentSession
    let isCurrent: Bool
    let isDropTarget: Bool
    var onChat: () -> Void
    var onAddChild: () -> Void
    var onRemove: () -> Void

    @State private var hovering = false

    var body: some View {
        VStack(spacing: 6) {
            HStack(spacing: 6) {
                Circle()
                    .fill(agent.isRunning ? .orange : (agent.isAlive ? .green : .gray))
                    .frame(width: 8, height: 8)
                    .help(agent.isRunning ? "Working"
                          : (agent.isAlive ? "Idle" : "Not running"))
                Text(agent.name)
                    .font(.callout.weight(isCurrent ? .bold : .semibold))
                    .lineLimit(1)
            }
            HStack(spacing: 4) {
                if !agent.role.isEmpty {
                    badge(agent.role, Comb.amber)
                }
                if agent.sudo {
                    badge("sudo", .red)
                }
                if agent.role.isEmpty && !agent.sudo {
                    Text(agent.personaRole ?? "worker")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.vertical, 10).padding(.horizontal, 14)
        .frame(minWidth: 130)
        .background(.background, in: RoundedRectangle(cornerRadius: 10))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(isDropTarget ? Comb.honey
                        : (isCurrent ? Comb.gold : Color.gray.opacity(0.35)),
                        lineWidth: (isDropTarget || isCurrent) ? 2.5 : 1)
        )
        .scaleEffect(isDropTarget ? 1.04 : 1.0)
        .animation(.easeOut(duration: 0.12), value: isDropTarget)
        .shadow(color: .black.opacity(0.08), radius: 2, y: 1)
        .overlay(alignment: .bottom) {
            if hovering {
                HStack(spacing: 10) {
                    Button { onChat() } label: {
                        Image(systemName: "bubble.left.fill")
                    }
                    .help("Open this agent's chat")
                    Button { onAddChild() } label: {
                        Image(systemName: "plus.circle.fill")
                    }
                    .help("Add a report under \(agent.name)")
                    Button { onRemove() } label: {
                        Image(systemName: "minus.circle.fill")
                    }
                    .help("Remove from the team")
                }
                .buttonStyle(.plain)
                .font(.callout)
                .foregroundStyle(Comb.amber)
                .padding(.horizontal, 8).padding(.vertical, 4)
                .background(.background, in: Capsule())
                .overlay(Capsule().stroke(Comb.gold.opacity(0.5), lineWidth: 1))
                .offset(y: 14)
            }
        }
        .onHover { hovering = $0 }
        .onTapGesture { onChat() }
        .contextMenu {
            Button("Open chat") { onChat() }
            Button("Add report") { onAddChild() }
            Divider()
            Button("Remove from team", role: .destructive) { onRemove() }
        }
    }

    private func badge(_ text: String, _ color: Color) -> some View {
        Text(text)
            .font(.caption2)
            .padding(.horizontal, 6).padding(.vertical, 1)
            .background(color.opacity(0.14), in: Capsule())
            .foregroundStyle(color)
    }
}

/// Name + role for a new tree member; the parent (if any) is fixed by the
/// card the sheet was opened from. Names pre-fill from the shared pool —
/// every agent is a person; roll the dice for a different one.
private struct AddTreeAgentSheet: View {
    let parent: AgentSession?
    var onCreate: (String, String?, String, String, Bool) -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var name = Names.random()
    @State private var personaRole = "worker"
    @State private var role = ""
    @State private var agentDescription = ""
    @State private var sudo = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(parent.map { "New report under \($0.name)" } ?? "New root agent")
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
            Picker("Persona", selection: $personaRole) {
                Text("Worker").tag("worker")
                Text("Planner").tag("planner")
                Text("Advisor").tag("advisor")
            }
            .pickerStyle(.segmented)
            Text(personaCaption)
                .font(.caption)
                .foregroundStyle(.secondary)
            TextField("Display role (optional)", text: $role)
            TextField("Description (optional)", text: $agentDescription, axis: .vertical)
                .lineLimit(2...4)
            Toggle("sudo — run tool calls without asking", isOn: $sudo)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Create") {
                    let n = name.trimmingCharacters(in: .whitespaces)
                    guard !n.isEmpty else { return }
                    onCreate(n, personaRole == "worker" ? nil : personaRole,
                             role, agentDescription, sudo)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(20)
        .frame(width: 440)
    }

    private var personaCaption: String {
        switch personaRole {
        case "planner":
            return "Stewards the ticket board: grooms, plans sprints, files "
                + "tickets instead of implementing."
        case "advisor":
            return "Tracks project costs and budget, keeps a ledger, and helps "
                + "set up accounting — advise/record only, no product code."
        default:
            return "A hands-on agent; it claims tickets under its own name as "
                + "it works."
        }
    }
}
