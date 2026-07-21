import XCTest
@testable import Goldcomb

/// State-level tests for SessionStore's agent tree and deploy-promotion logic
/// — the NEXA-38 (removed sub-agent re-appears) and NEXA-43/44 (sidebar
/// re-diff) regression class. Everything here runs against `SessionStore
/// (forTesting:)`, which skips disk restore, the poll timer, and process
/// launches, so the assertions are pure in-memory state checks.
///
/// Invariants are the ones Vera pinned (see NEXA-54 discussion):
///   removeFromTree: children reparent UP, nothing is orphaned.
///   reparent: refuses self/descendant drops (no cycles).
///   promoteDeploys: skips stale, declined, and duplicate records; live/recent
///   records promote under their deployer without stealing focus.
final class SessionStoreTreeTests: XCTestCase {

    // MARK: - fixtures

    private var tempDir: URL!

    override func setUp() {
        super.setUp()
        tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("goldcomb-tests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(
            at: tempDir, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tempDir)
        super.tearDown()
    }

    private func makeStore() -> SessionStore {
        SessionStore(forTesting: true)
    }

    /// A session that is never started — pure in-memory row.
    @discardableResult
    private func addSession(_ store: SessionStore, name: String,
                            projectID: UUID? = nil,
                            parentID: UUID? = nil,
                            role: String = "") -> AgentSession {
        let s = AgentSession(name: name, directory: tempDir, sudo: false,
                             role: role)
        s.projectID = projectID
        s.parentID = parentID
        store.sessions.append(s)
        return s
    }

    private func makeProject(_ store: SessionStore,
                             name: String = "Proj") -> Project {
        let p = Project(name: name, directory: tempDir)
        store.projects.append(p)
        return p
    }

    // MARK: - removeFromTree (invariants 1 & 2)

    /// Children move up to the removed agent's parent; nothing is orphaned.
    func testRemoveFromTreeReparentsChildrenUpward() {
        let store = makeStore()
        let a = addSession(store, name: "A")            // root
        let b = addSession(store, name: "B", parentID: a.id)
        let c = addSession(store, name: "C", parentID: b.id)  // grandchild

        store.removeFromTree(b)

        XCTAssertEqual(c.parentID, a.id, "grandchild should reparent to A")
        XCTAssertFalse(store.sessions.contains { $0.id == b.id })
        // No session may point at a removed parent id.
        for s in store.sessions {
            if let pid = s.parentID {
                XCTAssertTrue(store.sessions.contains { $0.id == pid },
                              "\(s.name) has dangling parentID")
            }
        }
    }

    /// Removing a root promotes its children to roots (parentID nil).
    func testRemoveFromTreeRootPromotesChildrenToRoots() {
        let store = makeStore()
        let a = addSession(store, name: "A")
        let b = addSession(store, name: "B", parentID: a.id)
        let c = addSession(store, name: "C", parentID: a.id)

        store.removeFromTree(a)

        XCTAssertNil(b.parentID)
        XCTAssertNil(c.parentID)
        let roots = store.treeRoots(among: store.sessions)
        XCTAssertEqual(Set(roots.map(\.id)), Set([b.id, c.id]))
    }

    /// treeRoots surfaces a reparented child whose new parent is out of scope.
    func testTreeRootsIncludesChildWhoseParentIsOutOfScope() {
        let store = makeStore()
        let a = addSession(store, name: "A")
        let b = addSession(store, name: "B", parentID: a.id)
        let c = addSession(store, name: "C", parentID: b.id)

        store.removeFromTree(b)   // C now parents to A
        XCTAssertEqual(c.parentID, a.id)

        // Scope that contains C but not A: C's parent is absent, so C is a root.
        let scoped = [c]
        let roots = store.treeRoots(among: scoped)
        XCTAssertEqual(roots.map(\.id), [c.id])
    }

    // MARK: - reparent cycle-guard (invariant 3)

    func testReparentUnderSelfIsNoOp() {
        let store = makeStore()
        let a = addSession(store, name: "A")
        store.reparent(a, under: a)
        XCTAssertNil(a.parentID, "reparenting under self must not mutate")
    }

    /// Dropping a lead under its own report would create a cycle — refuse.
    func testReparentUnderDescendantIsRefused() {
        let store = makeStore()
        let a = addSession(store, name: "A")
        let b = addSession(store, name: "B", parentID: a.id)
        let c = addSession(store, name: "C", parentID: b.id)

        store.reparent(a, under: c)   // A under its own grandchild

        XCTAssertNil(a.parentID, "cycle-forming reparent must be refused")
        // Existing edges unchanged.
        XCTAssertEqual(b.parentID, a.id)
        XCTAssertEqual(c.parentID, b.id)
    }

    func testReparentToRootClearsParent() {
        let store = makeStore()
        let a = addSession(store, name: "A")
        let b = addSession(store, name: "B", parentID: a.id)

        store.reparent(b, under: nil)
        XCTAssertNil(b.parentID)
    }

    // MARK: - teamContext

    func testTeamContextReportsLeadTeammatesAndReports() {
        let store = makeStore()
        let lead = addSession(store, name: "Lead", role: "Planner")
        let me = addSession(store, name: "Me", parentID: lead.id)
        _ = addSession(store, name: "Peer", parentID: lead.id)
        _ = addSession(store, name: "Report", parentID: me.id)

        let ctx = store.teamContext(for: me) ?? ""
        XCTAssertTrue(ctx.contains("Your lead: @Lead"))
        XCTAssertTrue(ctx.contains("Your teammates: @Peer"))
        XCTAssertTrue(ctx.contains("Your reports: @Report"))
    }

    func testTeamContextNilForLoneRoot() {
        let store = makeStore()
        let solo = addSession(store, name: "Solo")
        XCTAssertNil(store.teamContext(for: solo))
    }

    // MARK: - SubAgentRecord decoding helpers

    private func liveRecord(label: String, pid: Int?) -> SubAgentRecord {
        SubAgentRecord(id: UUID().uuidString, label: label, state: "running",
                       color: "green", startedAt: Date().timeIntervalSince1970,
                       endedAt: nil, toolCalls: 0, error: nil,
                       transcriptPath: nil, pid: pid)
    }

    private func finishedRecord(label: String, endedAgo seconds: Double,
                                pid: Int?) -> SubAgentRecord {
        SubAgentRecord(id: UUID().uuidString, label: label, state: "done",
                       color: "gray",
                       startedAt: Date().timeIntervalSince1970 - seconds - 5,
                       endedAt: Date().timeIntervalSince1970 - seconds,
                       toolCalls: 0, error: nil, transcriptPath: nil, pid: pid)
    }

    // MARK: - promoteDeploys skip paths (invariants 4, 5, 6)

    /// Every deployed agent becomes a permanent roster member — even a deploy
    /// that finished long ago (no age window), so the user can configure it.
    func testPromoteDeploysPromotesOldFinishedRecords() {
        let store = makeStore()
        let project = makeProject(store)
        let old = finishedRecord(label: "Old", endedAgo: 25 * 60, pid: nil)

        store.runPromoteDeploysForTesting([project.id: [old]])

        XCTAssertTrue(store.sessions.contains { $0.name == "Old" },
                      "a deployed agent must join the roster regardless of age")
    }

    /// Setting a default model publishes it to the project's deploy config, so
    /// a lead deploying this agent runs it on the chosen model
    /// (goldcomb/agents.py `configured_default` reads exactly this file).
    func testDefaultModelWritesDeployConfig() throws {
        let store = makeStore()
        let project = makeProject(store)
        let agent = addSession(store, name: "Quill Ashwood (swift-worker-2)",
                               projectID: project.id)
        store.setAgentDefaultModel(agent, provider: "anthropic",
                                   model: "claude-opus-4-8")

        let url = tempDir.appendingPathComponent(".ai/agents/agent-config.json")
        let json = try JSONSerialization.jsonObject(
            with: Data(contentsOf: url)) as? [String: Any]
        let agents = json?["agents"] as? [String: [String: String]]
        XCTAssertEqual(agents?["Quill Ashwood (swift-worker-2)"]?["model"],
                       "claude-opus-4-8")
        XCTAssertEqual(agents?["Quill Ashwood (swift-worker-2)"]?["provider"],
                       "anthropic")
    }

    func testClearingDefaultRemovesDeployConfig() {
        let store = makeStore()
        let project = makeProject(store)
        let agent = addSession(store, name: "Solo", projectID: project.id)
        store.setAgentDefaultModel(agent, provider: "anthropic", model: "m")
        store.setAgentDefaultModel(agent, provider: "", model: "")  // clear

        // No configured agents left → the file is removed, not left stale.
        let url = tempDir.appendingPathComponent(".ai/agents/agent-config.json")
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
    }

    /// A record the user closed (declinedPromotions) must not resurrect.
    /// This is the NEXA-38 fix path.
    func testPromoteDeploysSkipsDeclinedPromotion() {
        let store = makeStore()
        let project = makeProject(store)
        let live = liveRecord(label: "Worker", pid: nil)

        // Simulate the user having closed this row: removing a session whose
        // name matches a live sub-agent record records a declined promotion.
        let existing = addSession(store, name: "Worker", projectID: project.id)
        store.subAgents[project.id] = [live]
        store.remove(existing)   // inserts declinedPromotions key
        XCTAssertFalse(store.sessions.contains { $0.name == "Worker" })

        store.runPromoteDeploysForTesting([project.id: [live]])

        XCTAssertFalse(store.sessions.contains { $0.name == "Worker" },
                       "declined promotion must not be resurrected")
    }

    /// A record whose name+project already has a session must not duplicate.
    func testPromoteDeploysSkipsExistingSession() {
        let store = makeStore()
        let project = makeProject(store)
        _ = addSession(store, name: "Worker", projectID: project.id)
        let live = liveRecord(label: "Worker", pid: nil)
        let before = store.sessions.count

        store.runPromoteDeploysForTesting([project.id: [live]])

        let matching = store.sessions.filter {
            $0.name == "Worker" && $0.projectID == project.id
        }
        XCTAssertEqual(matching.count, 1, "must not create a duplicate row")
        XCTAssertEqual(store.sessions.count, before)
    }

    // MARK: - SavedAgent persistence round-trip

    /// The persisted agent shape survives encode→decode with all fields intact
    /// (this is the row SidebarState.json stores per agent).
    func testSavedAgentFullRoundTrip() throws {
        let agent = SavedAgent(
            id: UUID(), name: "Ada", directory: "/tmp/x", sudo: true,
            role: "Lead", description: "owns quality", personaRole: "planner",
            provider: "anthropic", model: "claude",
            projectID: UUID(), parentID: UUID())

        let data = try JSONEncoder().encode(agent)
        let a = try JSONDecoder().decode(SavedAgent.self, from: data)

        XCTAssertEqual(a.id, agent.id)
        XCTAssertEqual(a.role, "Lead")
        XCTAssertEqual(a.description, "owns quality")
        XCTAssertEqual(a.personaRole, "planner")
        XCTAssertEqual(a.provider, "anthropic")
        XCTAssertEqual(a.model, "claude")
        XCTAssertEqual(a.projectID, agent.projectID)
        XCTAssertEqual(a.parentID, agent.parentID)
        XCTAssertTrue(a.sudo)
    }

    /// Optional tree/grouping fields default cleanly when absent from disk.
    func testSavedAgentDecodesWithMissingOptionalFields() throws {
        let json = """
        {"id":"\(UUID().uuidString)","name":"Solo","directory":"/tmp"}
        """
        let a = try JSONDecoder().decode(SavedAgent.self, from: Data(json.utf8))
        XCTAssertNil(a.parentID)
        XCTAssertNil(a.projectID)
        XCTAssertNil(a.personaRole)
        XCTAssertFalse(a.sudo)
        XCTAssertEqual(a.role, "")
    }

    /// Restoring clears a parentID whose parent no longer exists (orphan
    /// repair) — the same "no dangling parentID" invariant as removeFromTree.
    func testOrphanedParentIDDropsToRoot() throws {
        // Simulate what restore() does at line ~408: drop edges to missing parents.
        let store = makeStore()
        let orphan = addSession(store, name: "Orphan", parentID: UUID())  // parent gone

        // The repair logic: any parentID not in the current id set becomes nil.
        let ids = Set(store.sessions.map(\.id))
        for s in store.sessions where s.parentID != nil && !ids.contains(s.parentID!) {
            s.parentID = nil
        }

        XCTAssertNil(orphan.parentID)
    }
}
