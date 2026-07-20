import SwiftUI

/// Ticket-id linkification for chat transcripts (NEXA-84). A token like
/// `NEXA-84` in a message becomes a tappable link; tapping routes the reader
/// to that project's Sprint view. Rendering is view-local and easy; the jump
/// needs shared navigation state (SessionStore.pendingTicketFocus), which is
/// why this waited on the sidebar-chats restructure.
enum ChatLinkRouter {
    /// Custom URL scheme carried by ticket links inside an AttributedString.
    static let scheme = "goldcomb"

    /// A board-style ticket id (`NEXA-84`, `ABC-1`): two-plus uppercase
    /// letters, a dash, and digits — the whole token.
    private static let pattern = try! NSRegularExpression(
        pattern: "^[A-Z]{2,}-[0-9]+$")

    /// The ticket id in a token, or nil. Trailing punctuation ("NEXA-84.") is
    /// stripped first so it still links.
    static func ticketID(in token: String) -> String? {
        let bare = token.trimmingCharacters(in: .punctuationCharacters)
        let range = NSRange(bare.startIndex..., in: bare)
        return pattern.firstMatch(in: bare, range: range) != nil ? bare : nil
    }

    static func url(for ticket: String) -> URL {
        URL(string: "\(scheme)://ticket/\(ticket)")!
    }

    /// The ticket id a link URL points at, or nil if it isn't one of ours.
    static func ticket(from url: URL) -> String? {
        guard url.scheme == scheme, url.host == "ticket" else { return nil }
        let id = url.lastPathComponent
        return ticketID(in: id) == id ? id : nil
    }

    /// Build the rendered message body: ticket ids become links, and the human
    /// handle ("user"/"you") stays highlighted so a lurker sees where input is
    /// wanted. Pure over its inputs, so the wiring is unit-testable without a
    /// view (see ChatLinkRouterTests).
    static func attributed(_ text: String,
                           linkColor: Color = .accentColor,
                           userColor: Color = .orange) -> AttributedString {
        var out = AttributedString()
        let tokens = text.components(separatedBy: " ")
        for (i, token) in tokens.enumerated() {
            var piece = AttributedString(token)
            let bare = token.trimmingCharacters(in: .punctuationCharacters)
            if bare == "user" || bare == "@user" || bare.lowercased() == "you" {
                piece.foregroundColor = userColor
                piece.inlinePresentationIntent = .stronglyEmphasized
            } else if let id = ticketID(in: token) {
                piece.link = url(for: id)
                piece.foregroundColor = linkColor
                piece.underlineStyle = .single
            }
            out += piece
            if i < tokens.count - 1 { out += AttributedString(" ") }
        }
        return out
    }
}
