import XCTest
@testable import Goldcomb

/// Per-agent default model: the model an agent launches on whenever its process
/// starts — so it uses its own model when woken for a group chat or delegated
/// to, not just when the user opens its chat. The launch-arg building and the
/// persistence semantics are pinned here; the menus are UI.
final class AgentDefaultModelTests: XCTestCase {

    private let dir = URL(fileURLWithPath: "/tmp")

    private func session(defaultProvider: String? = nil,
                         defaultModel: String? = nil) -> AgentSession {
        AgentSession(name: "Quill", directory: dir, sudo: false,
                     defaultProvider: defaultProvider, defaultModel: defaultModel)
    }

    // MARK: launch arguments

    func testDefaultModelIsPassedAtLaunch() {
        let s = session(defaultProvider: "anthropic", defaultModel: "claude-opus-4-8")
        let args = s.serveArguments(baseArgs: ["-m", "goldcomb"])
        XCTAssertTrue(args.contains("--serve"))
        XCTAssertTrue(args.contains("--agent-name"))
        // The provider/model land as a --provider/--model pair.
        let pi = args.firstIndex(of: "--provider")
        let mi = args.firstIndex(of: "--model")
        XCTAssertNotNil(pi); XCTAssertNotNil(mi)
        XCTAssertEqual(args[pi! + 1], "anthropic")
        XCTAssertEqual(args[mi! + 1], "claude-opus-4-8")
    }

    func testNoDefaultMeansNoModelFlags() {
        // No configured default → inherit the app's global model (no flags).
        let args = session().serveArguments(baseArgs: ["-m", "goldcomb"])
        XCTAssertFalse(args.contains("--provider"))
        XCTAssertFalse(args.contains("--model"))
    }

    func testEmptyDefaultIsTreatedAsUnset() {
        let args = session(defaultProvider: "", defaultModel: "")
            .serveArguments(baseArgs: ["-m", "goldcomb"])
        XCTAssertFalse(args.contains("--provider"))
        XCTAssertFalse(args.contains("--model"))
    }

    // MARK: store — set / clear / live-vs-default

    func testSetDefaultModelPersistsOnTheSession() {
        let store = SessionStore(forTesting: true)
        let s = session()
        store.sessions.append(s)
        store.setAgentDefaultModel(s, provider: "gemini", model: "gemini-2.5-flash")
        XCTAssertEqual(s.defaultProvider, "gemini")
        XCTAssertEqual(s.defaultModel, "gemini-2.5-flash")
    }

    func testEmptyProviderClearsTheDefault() {
        let store = SessionStore(forTesting: true)
        let s = session(defaultProvider: "gemini", defaultModel: "gemini-2.5-flash")
        store.sessions.append(s)
        store.setAgentDefaultModel(s, provider: "", model: "")
        XCTAssertNil(s.defaultProvider)
        XCTAssertNil(s.defaultModel)
    }

    func testUpdateAgentMetadataEditsRoleAndDescription() {
        // The config sheet edits display-only metadata live (no restart) — it's
        // never passed to the process, only shown in the UI.
        let store = SessionStore(forTesting: true)
        let s = AgentSession(name: "Quill", directory: dir, sudo: false,
                             role: "old", description: "old notes")
        store.sessions.append(s)
        store.updateAgentMetadata(s, role: "  Reviewer  ", description: " owns tests ")
        XCTAssertEqual(s.role, "Reviewer")            // trimmed
        XCTAssertEqual(s.description, "owns tests")
        // Metadata is not part of the launch args (unlike the default model).
        let args = s.serveArguments(baseArgs: [])
        XCTAssertFalse(args.contains("Reviewer"))
    }

    func testLiveModelIsDistinctFromDefault() {
        // The live provider/model (what the chip shows / changes) is separate
        // from the configured default; changing one must not be read as the
        // other. The default stays whatever was configured.
        let s = session(defaultProvider: "anthropic", defaultModel: "claude-opus-4-8")
        s.provider = "openai"     // as if a live `using` event arrived
        s.model = "gpt-4o"
        XCTAssertEqual(s.defaultProvider, "anthropic")
        XCTAssertEqual(s.defaultModel, "claude-opus-4-8")
        // And the launch args still reflect the default, not the live model.
        let args = s.serveArguments(baseArgs: [])
        XCTAssertTrue(args.contains("claude-opus-4-8"))
        XCTAssertFalse(args.contains("gpt-4o"))
    }
}
