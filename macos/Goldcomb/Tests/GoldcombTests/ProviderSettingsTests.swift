import XCTest
@testable import Goldcomb

final class ProviderSettingsTests: XCTestCase {
    func testListingDecodesRedactedProviderState() throws {
        let json = #"{"config_revision":7,"current":{"provider":"work","model":"gpt-x"},"providers":[{"name":"work","type":"openai","base_url":"","default_model":"gpt-x","has_key":true,"key_source":"env"}]}"#
        let listing = try JSONDecoder().decode(ProviderListing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.config_revision, 7)
        XCTAssertEqual(listing.current["provider"], "work")
        XCTAssertEqual(listing.providers.first?.key_source, "env")
        XCTAssertEqual(listing.providers.first?.default_model, "gpt-x")
    }

    func testUpdateArgumentsRenameAndSendKeyOnlyThroughStdinFlag() {
        let draft = ProviderDraft(originalName: "old", name: "new", type: "openai",
                                  apiKey: "super-secret", baseURL: "https://api.example/v1",
                                  defaultModel: "gpt-x", makeCurrent: true)
        let args = ProviderSettingsModel.commandArguments(action: "update", draft: draft)
        XCTAssertTrue(args.contains("--api-key-stdin"))
        XCTAssertFalse(args.contains("super-secret"))
        XCTAssertEqual(Array(args.prefix(3)), ["config", "update", "--json"])
        XCTAssertTrue(args.contains("--new-name"))
        XCTAssertTrue(args.contains("new"))
        XCTAssertTrue(args.contains("--current"))
    }

    func testCommandInvocationPreservesQuotedExecutableAndArguments() throws {
        let previous = AppSettings.command
        defer { AppSettings.command = previous }
        AppSettings.command = #"'/Applications/My Goldcomb/python' -m goldcomb"#
        let invocation = try AppSettings.commandInvocation()
        XCTAssertEqual(invocation.executable, "/Applications/My Goldcomb/python")
        XCTAssertEqual(invocation.arguments, ["-m", "goldcomb"])
    }

    func testStaleRevisionRequiresLiveAgentAndClearsWhenRevisionsMatch() {
        let agent = AgentSession(name: "test", directory: URL(fileURLWithPath: "/tmp"),
                                 sudo: false)
        agent.readyConfigRevision = 3
        agent.currentConfigRevision = 4
        agent.isAlive = true
        XCTAssertTrue(agent.hasStaleConfig)
        agent.readyConfigRevision = 4
        XCTAssertFalse(agent.hasStaleConfig)
        agent.readyConfigRevision = 3
        agent.isAlive = false
        XCTAssertFalse(agent.hasStaleConfig)
    }
}
