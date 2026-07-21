import Foundation

/// Shared display-name formatting for chat surfaces (NEXA-115). This was
/// `shortName(_:)`, duplicated verbatim in ChatRoomView and ChatsTabView —
/// one home now so the two stacks can't drift on it again.
enum NameFormatting {

    /// Short display name for a chat participant: "Quill Ashwood
    /// (swift-worker-2)" → "Quill Ashwood", and the human handle "user" →
    /// "You". The parenthesized suffix is the agent's role/tool annotation,
    /// dropped for tight rows and bubble headers.
    static func shortName(_ name: String) -> String {
        name == "user" ? "You"
            : String(name.split(separator: "(").first ?? Substring(name))
                .trimmingCharacters(in: .whitespaces)
    }
}
