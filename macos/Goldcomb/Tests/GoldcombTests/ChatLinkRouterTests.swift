import XCTest
@testable import Goldcomb

/// Ticket-id linkification (NEXA-84). The parsing and URL round-trip are pure,
/// so they're pinned here; the cross-tab jump they drive is exercised via the
/// store in SessionStore tests.
final class ChatLinkRouterTests: XCTestCase {

    func testDetectsTicketIDs() {
        XCTAssertEqual(ChatLinkRouter.ticketID(in: "NEXA-84"), "NEXA-84")
        XCTAssertEqual(ChatLinkRouter.ticketID(in: "ABC-1"), "ABC-1")
        // Trailing punctuation is stripped so "NEXA-84." still links.
        XCTAssertEqual(ChatLinkRouter.ticketID(in: "NEXA-84."), "NEXA-84")
        XCTAssertEqual(ChatLinkRouter.ticketID(in: "(NEXA-7)"), "NEXA-7")
    }

    func testRejectsNonTickets() {
        XCTAssertNil(ChatLinkRouter.ticketID(in: "NEXA"))       // no number
        XCTAssertNil(ChatLinkRouter.ticketID(in: "nexa-1"))     // lowercase
        XCTAssertNil(ChatLinkRouter.ticketID(in: "A-1"))        // one letter
        XCTAssertNil(ChatLinkRouter.ticketID(in: "read_file"))
        XCTAssertNil(ChatLinkRouter.ticketID(in: "v1-2"))
    }

    func testURLRoundTrip() {
        let url = ChatLinkRouter.url(for: "NEXA-84")
        XCTAssertEqual(ChatLinkRouter.ticket(from: url), "NEXA-84")
    }

    func testForeignURLsAreNotTickets() {
        XCTAssertNil(ChatLinkRouter.ticket(from: URL(string: "https://example.com")!))
        XCTAssertNil(ChatLinkRouter.ticket(from: URL(string: "goldcomb://agent/x")!))
    }

    func testAttributedBodyLinksTicketsAndHighlightsUser() {
        let s = ChatLinkRouter.attributed("user please see NEXA-84 now")
        // The ticket run carries a link to its ticket URL.
        let linked = s.runs.first { $0.link != nil }
        XCTAssertEqual(linked?.link, ChatLinkRouter.url(for: "NEXA-84"))
        // "user" is emphasized (the addressed-human highlight), not a link.
        let userRun = s.runs.first { s[$0.range].characters.starts(with: "user") }
        XCTAssertNotNil(userRun?.inlinePresentationIntent)
    }

    func testPlainTextHasNoLinks() {
        let s = ChatLinkRouter.attributed("just a normal message")
        XCTAssertNil(s.runs.first { $0.link != nil })
    }

    func testDecorateComposesOnExistingBoldRuns() {
        // The Markdown path hands decorate an already-styled AttributedString;
        // ticket links must be added without disturbing the bold run.
        var attr = AttributedString("see NEXA-84 now")
        if let r = attr.range(of: "see") {
            attr[r].inlinePresentationIntent = .stronglyEmphasized
        }
        let out = ChatLinkRouter.decorate(attr)
        // The pre-existing bold survives.
        let bold = out.runs.first { out[$0.range].characters.starts(with: "see") }
        XCTAssertNotNil(bold?.inlinePresentationIntent)
        // And the ticket became a link.
        XCTAssertEqual(out.runs.first { $0.link != nil }?.link,
                       ChatLinkRouter.url(for: "NEXA-84"))
    }

    func testDecoratePreservesPlainTextContent() {
        // Attribute-only: the visible characters are unchanged.
        let out = ChatLinkRouter.decorate(AttributedString("ping user re NEXA-9"))
        XCTAssertEqual(String(out.characters), "ping user re NEXA-9")
    }
}
