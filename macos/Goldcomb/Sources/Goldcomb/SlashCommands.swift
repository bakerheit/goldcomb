import SwiftUI

/// A composer slash command in the macOS app — a quick action you invoke by
/// typing `/name` (or picking it from the palette) instead of sending a
/// message. Each maps to an existing AgentSession command, so the app and CLI
/// stay in step. Zero-argument by design; anything needing arguments belongs
/// in a real UI affordance, not a text command.
struct SlashCommand: Identifiable {
    let name: String            // without the leading slash
    let subtitle: String
    let icon: String            // SF Symbol
    let run: (AgentSession) -> Void
    /// When the command can fire. Default: a live, idle session.
    var isEnabled: (AgentSession) -> Bool = { $0.isAlive && !$0.isRunning }

    var id: String { name }
}

enum SlashCommands {
    /// The available commands, in palette order.
    static let all: [SlashCommand] = [
        SlashCommand(
            name: "compact",
            subtitle: "Summarize history to shrink context (keeps the thread)",
            icon: "arrow.down.right.and.arrow.up.left",
            run: { $0.compact() }),
        SlashCommand(
            name: "clear",
            subtitle: "Start a fresh conversation (the current one is saved)",
            icon: "square.and.pencil",
            run: { $0.clearConversation() }),
        SlashCommand(
            name: "sudo",
            subtitle: "Toggle auto-approving tool calls",
            icon: "checkmark.shield",
            run: { $0.setSudo(!$0.sudo) },
            // A toggle is fine mid-turn; it only needs a live process.
            isEnabled: { $0.isAlive }),
    ]

    /// The command a composer line invokes, if it's a bare `/name` (optionally
    /// with surrounding whitespace). Returns nil for a normal message — so a
    /// message that merely *starts* with a word beginning in "/" isn't
    /// swallowed. Matching is case-insensitive.
    static func match(_ text: String) -> SlashCommand? {
        let t = text.trimmingCharacters(in: .whitespaces)
        guard t.hasPrefix("/") else { return nil }
        let name = String(t.dropFirst()).lowercased()
        return all.first { $0.name == name }
    }

    /// Commands to offer while the user is typing `/xxx`, for the palette.
    /// Empty once a space is typed (the line is becoming a message) or when the
    /// text isn't a slash prefix.
    static func suggestions(for text: String) -> [SlashCommand] {
        let t = text.trimmingCharacters(in: .whitespaces)
        guard t.hasPrefix("/"), !t.dropFirst().contains(" ") else { return [] }
        let partial = String(t.dropFirst()).lowercased()
        return all.filter { partial.isEmpty || $0.name.hasPrefix(partial) }
    }
}
