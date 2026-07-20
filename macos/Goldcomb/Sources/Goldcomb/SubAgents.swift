import SwiftUI

/// A deployed sub-agent's on-disk record (`.ai/agents/<id>.json`) — written
/// live by the worker's process so out-of-process readers (us) can watch it.
struct SubAgentRecord: Identifiable, Equatable {
    let id: String
    let label: String
    let state: String
    let color: String
    let startedAt: Double
    let endedAt: Double?
    let toolCalls: Int
    let error: String?
    let transcriptPath: String?
    /// pid of the process that ran this deploy (the lead agent's server).
    let pid: Int?

    var isLive: Bool { state == "starting" || state == "running" }

    var dotColor: Color {
        switch color {
        case "blue": .blue
        case "green": .green
        case "yellow": .yellow
        case "red": .red
        default: .gray
        }
    }

    static func loadAll(projectDir: URL) -> [SubAgentRecord] {
        let dir = projectDir.appendingPathComponent(".ai/agents")
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: nil
        ) else { return [] }
        var out: [SubAgentRecord] = []
        for url in files where url.pathExtension == "json" {
            guard let data = try? Data(contentsOf: url),
                  let d = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let id = d["id"] as? String
            else { continue }
            out.append(SubAgentRecord(
                id: id,
                label: d["label"] as? String ?? id,
                state: d["state"] as? String ?? "?",
                color: d["color"] as? String ?? "gray",
                startedAt: d["started_at"] as? Double ?? 0,
                endedAt: d["ended_at"] as? Double,
                toolCalls: d["n_tool_calls"] as? Int ?? 0,
                error: d["error"] as? String,
                transcriptPath: d["transcript_path"] as? String,
                pid: d["pid"] as? Int
            ))
        }
        return out.sorted { $0.startedAt > $1.startedAt }
    }
}

/// Sidebar rows for a project's deployed workers: live ones always, finished
/// ones only briefly — deploys are ephemeral, the sidebar shouldn't become a
/// graveyard. Data comes from the store's poller (never poll here: an empty
/// ForEach fires no lifecycle events).
struct SubAgentRows: View {
    let project: Project
    /// Passed down from the List builder — the data dependency MUST live in
    /// the List's own body: a nested @EnvironmentObject read invalidates the
    /// child but does not re-diff NSTableView rows until user interaction.
    let records: [SubAgentRecord]
    @EnvironmentObject var store: SessionStore

    /// Finished deploys linger this long before dropping off the sidebar.
    private static let lingerSeconds: Double = 20 * 60

    private var visible: [SubAgentRecord] {
        let cutoff = Date().timeIntervalSince1970 - Self.lingerSeconds
        let promoted = Set(store.sessions
            .filter { $0.projectID == project.id }.map(\.name))
        return records
            .filter { record in
                // Records surface here only until promotion gives them a
                // real agent row (normally within one poll tick of deploy
                // start) — the promoted row carries the blue busy indicator.
                !promoted.contains(record.label)
                    && (record.isLive || (record.endedAt ?? 0) > cutoff)
            }
            .prefix(6).map { $0 }
    }

    var body: some View {
        Group {
            ForEach(visible) { record in
                HStack(spacing: 6) {
                    Image(systemName: "bolt")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    Circle()
                        .fill(record.dotColor)
                        .frame(width: 6, height: 6)
                    Text(record.label)
                        .font(.caption)
                        .lineLimit(1)
                        .foregroundStyle(record.isLive ? .primary : .secondary)
                    Spacer(minLength: 0)
                    Text(record.state.replacingOccurrences(of: "_", with: " "))
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                .padding(.leading, 26)
                .listRowSeparator(.hidden)
                .selectionDisabled()
                .help(helpText(record))
                .contextMenu {
                    if let path = record.transcriptPath {
                        Button("Copy transcript path") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(path, forType: .string)
                        }
                    }
                    Button("Copy run id") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(record.id, forType: .string)
                    }
                }
            }
        }
    }

    private func helpText(_ r: SubAgentRecord) -> String {
        var bits = ["Deployed sub-agent — \(r.state)", "\(r.toolCalls) tool calls"]
        if let error = r.error { bits.append("error: \(error)") }
        return bits.joined(separator: " · ")
    }
}
