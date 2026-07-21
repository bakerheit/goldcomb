import Foundation

/// Parsing for the group-chat composer's inline `@`-mention autocomplete: given
/// the draft text, is the user mid-mention, and which agents match? Pure and
/// deterministic, so the popup's trigger and filtering are unit-tested without
/// SwiftUI.
enum MentionAutocomplete {
    /// The active `@…` the user is typing at the *end* of `text`: an `@` at a
    /// word boundary (start of string or after whitespace, and not part of an
    /// email/`@@`) followed by non-space characters to the end of the string.
    /// Returns the range of the whole `@partial` run (so it can be replaced)
    /// and the partial query after the `@` (empty right after typing `@`), or
    /// nil when the user isn't mid-mention (e.g. a space closed it).
    static func active(in text: String)
        -> (range: Range<String.Index>, query: String)? {
        guard let r = text.range(of: "(?<![\\w@])@[^\\s@]*$",
                                 options: .regularExpression) else { return nil }
        let query = String(text[text.index(after: r.lowerBound)..<r.upperBound])
        return (r, query)
    }

    /// The given (first) name — what the broker matches an `@tag` against, and
    /// what gets inserted.
    static func given(_ name: String) -> String {
        name.split(separator: " ").first.map(String.init) ?? name
    }

    /// Agents whose given name matches `query` (case-insensitive substring),
    /// prefix matches first, original order preserved among equals. An empty
    /// query (just typed `@`) returns everyone.
    static func suggestions(_ agents: [String], matching query: String)
        -> [String] {
        let q = query.lowercased()
        return agents.enumerated()
            .filter { q.isEmpty || given($0.element).lowercased().contains(q) }
            .sorted { l, r in
                let lp = given(l.element).lowercased().hasPrefix(q)
                let rp = given(r.element).lowercased().hasPrefix(q)
                if lp != rp { return lp }        // prefix matches rank first
                return l.offset < r.offset        // stable otherwise
            }
            .map(\.element)
    }

    /// Replace the active `@partial` (or append, if none) with `@Given ` in
    /// `text`, returning the new string. Used both by the popup and the button.
    static func applying(_ agentName: String, to text: String) -> String {
        let insert = "@\(given(agentName)) "
        var out = text
        if let (range, _) = active(in: text) {
            out.replaceSubrange(range, with: insert)
        } else {
            if !out.isEmpty && !out.hasSuffix(" ") { out += " " }
            out += insert
        }
        return out
    }
}
