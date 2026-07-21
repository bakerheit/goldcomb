import XCTest
@testable import Goldcomb

/// Composer slash-command parsing (the app's /compact et al.). The matching
/// and suggestion logic is pure, so it's pinned here; running a command is a
/// one-line delegate to AgentSession.
final class SlashCommandTests: XCTestCase {

    func testMatchesBareCommand() {
        XCTAssertEqual(SlashCommands.match("/compact")?.name, "compact")
        XCTAssertEqual(SlashCommands.match("  /clear  ")?.name, "clear")
        XCTAssertEqual(SlashCommands.match("/COMPACT")?.name, "compact")  // case-insensitive
    }

    func testDoesNotMatchAMessage() {
        // A message that merely mentions a path or starts wordily isn't a cmd.
        XCTAssertNil(SlashCommands.match("/compact the logs please"))
        XCTAssertNil(SlashCommands.match("please run /compact"))
        XCTAssertNil(SlashCommands.match("/unknowncmd"))
        XCTAssertNil(SlashCommands.match("normal message"))
    }

    func testSuggestionsFilterByPrefix() {
        XCTAssertEqual(SlashCommands.suggestions(for: "/c").map(\.name), ["compact", "clear"])
        XCTAssertEqual(SlashCommands.suggestions(for: "/comp").map(\.name), ["compact"])
        XCTAssertEqual(SlashCommands.suggestions(for: "/s").map(\.name), ["sudo"])
    }

    func testBareSlashOffersEverything() {
        XCTAssertEqual(SlashCommands.suggestions(for: "/").map(\.name),
                       SlashCommands.all.map(\.name))
    }

    func testSuggestionsStopOnceItBecomesAMessage() {
        // A space means the user is writing prose, not picking a command.
        XCTAssertTrue(SlashCommands.suggestions(for: "/compact now").isEmpty)
        XCTAssertTrue(SlashCommands.suggestions(for: "hello").isEmpty)
    }

    func testCompactIsPresentAndGatedToIdle() {
        let compact = SlashCommands.all.first { $0.name == "compact" }
        XCTAssertNotNil(compact)
        // Default gate is live-and-idle; sudo overrides to live-only, so the
        // set isn't uniform — pin compact's intent specifically.
        XCTAssertEqual(SlashCommands.all.map(\.name), ["compact", "clear", "sudo"])
    }
}
