import Foundation

/// Pure send-button policy for the shared composer (NEXA-110): given a draft,
/// whether anything is staged, and the mode's extra gate (agent liveness for
/// user↔agent chat; always on in rooms), can the send go through? Lifted out
/// of the two composers — which had drifted on how emptiness is measured — so
/// both stacks share one definition and it's testable without SwiftUI.
enum ComposerSendRules {
    /// A draft counts as text once it has any non-whitespace character.
    static func hasText(_ draft: String) -> Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Send is allowed with text or staged attachments (or both), subject to
    /// the mode's own gate (e.g. a live, idle agent process).
    static func canSend(draft: String, staged: Int, modeGate: Bool = true) -> Bool {
        modeGate && (hasText(draft) || staged > 0)
    }
}
