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
        decorate(AttributedString(text), linkColor: linkColor, userColor: userColor)
    }

    /// Add ticket-id links and the @user highlight to an already-built
    /// AttributedString *in place* — attribute-only, so it composes on top of
    /// Markdown-parsed runs (bold, italic, code) without disturbing them. This
    /// is how those chat affordances survive block Markdown rendering: the
    /// MarkdownMessage view passes this as its `decorate` hook.
    static func decorate(_ input: AttributedString,
                         linkColor: Color = .accentColor,
                         userColor: Color = .orange) -> AttributedString {
        var attr = input
        let plain = String(attr.characters)

        // A ticket id anywhere → a link. Two-plus caps, dash, digits.
        for (range, id) in ranges("\\b[A-Z]{2,}-[0-9]+\\b", in: plain, of: attr) {
            attr[range].link = url(for: id)
            attr[range].foregroundColor = linkColor
            attr[range].underlineStyle = .single
        }
        // The human handle, so a lurker sees where input is wanted.
        for (range, _) in ranges("(?i)(?<![\\w@])@?(?:user|you)\\b", in: plain, of: attr) {
            attr[range].foregroundColor = userColor
            attr[range].inlinePresentationIntent = .stronglyEmphasized
        }
        return attr
    }

    /// Regex matches mapped to AttributedString index ranges, by *character*
    /// distance (so emoji / combining marks stay aligned). Ranges are computed
    /// before any mutation; attribute-only edits don't shift indices, so
    /// applying them afterward is safe.
    private static func ranges(_ pattern: String, in plain: String,
                               of attr: AttributedString)
        -> [(Range<AttributedString.Index>, String)] {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return [] }
        let ns = NSRange(plain.startIndex..<plain.endIndex, in: plain)
        let chars = attr.characters
        var out: [(Range<AttributedString.Index>, String)] = []
        for m in re.matches(in: plain, range: ns) {
            guard let sr = Range(m.range, in: plain) else { continue }
            let lo = plain.distance(from: plain.startIndex, to: sr.lowerBound)
            let hi = plain.distance(from: plain.startIndex, to: sr.upperBound)
            guard let a = chars.index(chars.startIndex, offsetBy: lo,
                                      limitedBy: chars.endIndex),
                  let b = chars.index(chars.startIndex, offsetBy: hi,
                                      limitedBy: chars.endIndex),
                  a < b else { continue }
            out.append((a..<b, String(plain[sr])))
        }
        return out
    }
}
