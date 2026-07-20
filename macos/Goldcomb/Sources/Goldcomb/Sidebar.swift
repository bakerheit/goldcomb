import SwiftUI

// MARK: - aurora design tokens
// The app wears the CLI's default identity: iris violet for the model's
// voice / primary accent, aurora teal for machinery and activity.

enum Comb {
    /// Golden orange — the goldcomb brand hue.
    static let gold = Color(red: 0xE8 / 255, green: 0xA3 / 255, blue: 0x3D / 255)
    static let amber = Color(red: 0xC9 / 255, green: 0x7B / 255, blue: 0x1E / 255)
    static let honey = Color(red: 0xF2 / 255, green: 0xC1 / 255, blue: 0x4E / 255)
    static let copper = Color(red: 0xD0 / 255, green: 0x81 / 255, blue: 0x48 / 255)

    static let wordmark = LinearGradient(
        colors: [honey, gold, amber], startPoint: .leading, endPoint: .trailing
    )

    /// Deterministic per-project tint: warm-forward, still distinguishable.
    static let projectPalette: [Color] = [
        gold, copper, honey, .brown, .teal, .green,
    ]

    static func tint(for name: String) -> Color {
        var hash = 5381
        for byte in name.utf8 { hash = (hash &* 33) &+ Int(byte) }
        return projectPalette[abs(hash) % projectPalette.count]
    }

    static func monogram(_ name: String) -> String {
        let words = name.split { !$0.isLetter && !$0.isNumber }
        if words.count >= 2 {
            return String(words[0].prefix(1) + words[1].prefix(1)).uppercased()
        }
        return String(name.trimmingCharacters(in: .whitespaces).prefix(2)).uppercased()
    }
}

// MARK: - sidebar header

/// Brand row at the top of the sidebar: the gradient wordmark, echoing the
/// CLI banner.
struct SidebarBrand: View {
    var body: some View {
        HStack(spacing: 6) {
            Text("goldcomb")
                .font(.system(.title3, design: .rounded).weight(.bold))
                .foregroundStyle(Comb.wordmark)
            Text("agents")
                .font(.caption)
                .foregroundStyle(.tertiary)
                .padding(.top, 3)
            Spacer()
        }
        .padding(.horizontal, 4)
        .padding(.bottom, 2)
    }
}

// MARK: - project card

/// One project in the sidebar: monogram avatar in the project's tint, live
/// meta line, attention badge, collapse chevron, and the actions menu.
struct ProjectCard: View {
    @EnvironmentObject var store: SessionStore
    @ObservedObject var project: Project
    @State private var hovering = false

    var body: some View {
        let agents = store.sessionsFor(project)
        let running = agents.filter(\.isRunning).count
        let needsYou = agents.contains {
            $0.pendingConfirm != nil || $0.pendingQuestions != nil
        }
        let tint = Comb.tint(for: project.name)
        let collapsed = store.collapsed.contains(project.id)

        HStack(spacing: 9) {
            monogram(tint: tint)
            VStack(alignment: .leading, spacing: 1) {
                Text(project.name)
                    .font(.callout.weight(.semibold))
                    .lineLimit(1)
                Text(meta(agents: agents.count, running: running))
                    .font(.caption2)
                    .foregroundStyle(running > 0 ? AnyShapeStyle(Comb.honey)
                                                 : AnyShapeStyle(.secondary))
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
            if needsYou {
                AttentionBadge()
            }
            if hovering {
                Menu {
                    projectActions
                } label: {
                    Image(systemName: "ellipsis")
                        .foregroundStyle(.secondary)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .help("Project actions")
            }
            if !agents.isEmpty {
                Button {
                    withAnimation(.snappy(duration: 0.18)) {
                        store.toggleCollapsed(project)
                    }
                } label: {
                    Image(systemName: "chevron.down")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(collapsed ? -90 : 0))
                }
                .buttonStyle(.plain)
                .help(collapsed ? "Show agents" : "Hide agents")
            }
        }
        .padding(.vertical, 3)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .contextMenu { projectActions }
    }

    private func monogram(tint: Color) -> some View {
        Text(Comb.monogram(project.name))
            .font(.system(size: 11, weight: .bold, design: .rounded))
            .foregroundStyle(.white)
            .frame(width: 26, height: 26)
            .background(
                LinearGradient(
                    colors: [tint, tint.opacity(0.7)],
                    startPoint: .topLeading, endPoint: .bottomTrailing
                ),
                in: RoundedRectangle(cornerRadius: 7, style: .continuous)
            )
    }

    private func meta(agents: Int, running: Int) -> String {
        if agents == 0 { return project.directory.lastPathComponent }
        var parts = ["\(agents) agent\(agents == 1 ? "" : "s")"]
        if running > 0 { parts.append("\(running) running") }
        return parts.joined(separator: " · ")
    }

    /// Shared by the hover ellipsis menu and the right-click context menu;
    /// actions are delivered to ContentView via notifications (state lives
    /// there).
    @ViewBuilder
    private var projectActions: some View {
        Button("New agent") {
            NotificationCenter.default.post(name: .newAgentRequested, object: project)
        }
        Button("Rename…") {
            NotificationCenter.default.post(name: .renameProjectRequested, object: project)
        }
        Divider()
        Button("Remove project…", role: .destructive) {
            NotificationCenter.default.post(name: .removeProjectRequested, object: project)
        }
    }
}

/// Amber "the agent needs you" marker: a tool approval or question is waiting.
struct AttentionBadge: View {
    var body: some View {
        Image(systemName: "exclamationmark.circle.fill")
            .font(.caption)
            .foregroundStyle(.orange)
            .symbolEffect(.pulse, options: .repeating)
            .help("An agent is waiting for your input")
    }
}

// MARK: - agent row

/// One agent under its project: a tinted rail ties it to the project, the
/// status dot pulses teal while working, and the second line shows what the
/// agent is doing right now.
struct AgentSidebarRow: View {
    @ObservedObject var session: AgentSession
    var tint: Color = Comb.gold
    /// The live deploy record currently driving this agent's identity, if
    /// any — a promoted worker whose deploy is still running shows blue
    /// ("created by another agent, busy right now") instead of idle green.
    /// Passed down from the List builder, same as SubAgentRows' records.
    var busyDeploy: SubAgentRecord? = nil

    var body: some View {
        HStack(spacing: 8) {
            RoundedRectangle(cornerRadius: 1)
                .fill(tint.opacity(0.28))
                .frame(width: 2)
                .padding(.vertical, 1)
            statusDot
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 5) {
                    Text(session.name)
                        .font(.callout)
                        .lineLimit(1)
                    if !session.role.isEmpty {
                        Text(session.role)
                            .font(.system(size: 9, weight: .bold))
                            .padding(.horizontal, 4).padding(.vertical, 1)
                            .background(tint.opacity(0.16), in: Capsule())
                            .foregroundStyle(tint)
                            .lineLimit(1)
                    }
                    if session.sudo {
                        Text("sudo")
                            .font(.system(size: 9, weight: .bold))
                            .padding(.horizontal, 4).padding(.vertical, 1)
                            .background(.orange.opacity(0.16), in: Capsule())
                            .foregroundStyle(.orange)
                    }
                    if session.hasStaleConfig {
                        Text("restart")
                            .font(.system(size: 9, weight: .bold))
                            .padding(.horizontal, 4).padding(.vertical, 1)
                            .background(.yellow.opacity(0.18), in: Capsule())
                            .foregroundStyle(.orange)
                            .help("Provider settings changed; restart this agent to apply them")
                    }
                }
                Text(subtitle)
                    .font(.caption2)
                    .foregroundStyle(session.isRunning ? AnyShapeStyle(Comb.honey)
                                                       : AnyShapeStyle(.secondary))
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
            if session.pendingConfirm != nil || session.pendingQuestions != nil {
                AttentionBadge()
            }
        }
        .padding(.leading, 10)
        .padding(.vertical, 2)
        .contentShape(Rectangle())
    }

    @ViewBuilder
    private var statusDot: some View {
        if session.pendingConfirm != nil || session.pendingQuestions != nil {
            Circle().fill(.orange).frame(width: 7, height: 7)
        } else if busyDeploy != nil {
            Circle()
                .fill(.blue)
                .frame(width: 7, height: 7)
                .symbolEffectPulse()
        } else if session.isRunning {
            Circle()
                .fill(Comb.honey)
                .frame(width: 7, height: 7)
                .symbolEffectPulse()
        } else {
            Circle()
                .fill(session.isAlive ? Color.green.opacity(0.85) : .gray.opacity(0.6))
                .frame(width: 7, height: 7)
        }
    }

    private var subtitle: String {
        if let deploy = busyDeploy {
            return "busy · \(deploy.state.replacingOccurrences(of: "_", with: " "))"
        }
        if !session.isAlive { return "exited" }
        if session.isRunning, let status = session.status, !status.isEmpty {
            return status
        }
        return "\(session.provider) · \(session.model)"
    }
}

/// One sub-agent nested under its parent agent's row. Non-selectable and
/// never tagged — it's purely informational, mirroring the deploy_agent
/// lifecycle events; a fixed third level (sub-agents can't deploy their own).
struct SubagentSidebarRow: View {
    let subagent: SubAgentInfo
    var tint: Color = Comb.gold

    var body: some View {
        HStack(spacing: 8) {
            RoundedRectangle(cornerRadius: 1)
                .fill(tint.opacity(0.16))
                .frame(width: 2)
                .padding(.vertical, 1)
            statusDot
            VStack(alignment: .leading, spacing: 1) {
                Text(subagent.label)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                if !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 4)
        }
        .padding(.leading, 22)
        .padding(.vertical, 1)
        .contentShape(Rectangle())
        .help(help)
    }

    @ViewBuilder
    private var statusDot: some View {
        let dot = Circle()
            .fill(subagent.status.color)
            .frame(width: 6, height: 6)
        if subagent.status == .running {
            dot.symbolEffectPulse()
        } else {
            dot
        }
    }

    private var subtitle: String {
        switch subagent.status {
        case .starting:
            return "starting…"
        case .running:
            return subagent.task
        case .completed, .error:
            var parts: [String] = []
            if let reason = subagent.stopReason { parts.append(reason) }
            if let calls = subagent.toolCalls {
                parts.append("\(calls) tool call\(calls == 1 ? "" : "s")")
            }
            return parts.joined(separator: " · ")
        }
    }

    private var help: String {
        if subagent.task.isEmpty { return subagent.label }
        return "\(subagent.label): \(subagent.task)"
    }
}

// MARK: - chat row

/// One chat room under its project in the sidebar (NEXA-66/71): kind icon
/// (group vs DM), title, an unread badge, and a paused/waiting marker. An
/// agent-only DM (no human participant) is shown read-only per NEXA-69.
struct ChatSidebarRow: View {
    let room: ChatRoom
    var tint: Color = Comb.gold

    var body: some View {
        let unread = ChatReadState.unread(room)
        HStack(spacing: 8) {
            RoundedRectangle(cornerRadius: 1)
                .fill(tint.opacity(0.28))
                .frame(width: 2)
                .padding(.vertical, 1)
            Image(systemName: room.kind == "dm"
                  ? "person.line.dotted.person" : "bubble.left.and.bubble.right")
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 16)
            Text(room.title)
                .font(.callout)
                .lineLimit(1)
                .foregroundStyle(room.isAgentOnly ? AnyShapeStyle(.secondary)
                                                  : AnyShapeStyle(.primary))
            Spacer(minLength: 4)
            if room.isPaused {
                Image(systemName: "hand.raised.fill")
                    .font(.caption2)
                    .foregroundStyle(Comb.honey)
                    .help("Paused — the agents are waiting for you")
            }
            if unread > 0 {
                Text("\(unread)")
                    .font(.system(size: 10, weight: .bold))
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Comb.gold.opacity(0.9), in: Capsule())
                    .foregroundStyle(.white)
            }
        }
        .padding(.leading, 10)
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .help(room.isAgentOnly ? "Agent-to-agent DM (read-only)" : room.title)
    }
}

/// Shapes can't take symbolEffect; approximate the pulse with a repeating
/// opacity breathe so the running dot reads as alive.
private struct PulseModifier: ViewModifier {
    @State private var dim = false

    func body(content: Content) -> some View {
        content
            .opacity(dim ? 0.35 : 1)
            .animation(.easeInOut(duration: 0.8).repeatForever(autoreverses: true),
                       value: dim)
            .onAppear { dim = true }
    }
}

extension View {
    func symbolEffectPulse() -> some View { modifier(PulseModifier()) }
}

// MARK: - footer

/// Aggregate strip pinned under the sidebar: fleet size and token totals.
struct SidebarFooter: View {
    @EnvironmentObject var store: SessionStore

    var body: some View {
        let running = store.sessions.filter(\.isRunning).count
        let tokIn = store.sessions.reduce(0) { $0 + $1.sessionIn }
        let tokOut = store.sessions.reduce(0) { $0 + $1.sessionOut }
        HStack(spacing: 6) {
            Circle()
                .fill(running > 0 ? Comb.honey : .gray.opacity(0.5))
                .frame(width: 6, height: 6)
            Text(summary(running: running))
                .font(.caption2)
                .foregroundStyle(.secondary)
            Spacer()
            if tokIn + tokOut > 0 {
                Text("⬆\(tokIn.formattedTokens) ⬇\(tokOut.formattedTokens)")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(.bar)
        .overlay(alignment: .top) { Divider() }
    }

    private func summary(running: Int) -> String {
        let n = store.sessions.count
        if n == 0 { return "no agents" }
        var text = "\(n) agent\(n == 1 ? "" : "s")"
        if running > 0 { text += " · \(running) running" }
        return text
    }
}
