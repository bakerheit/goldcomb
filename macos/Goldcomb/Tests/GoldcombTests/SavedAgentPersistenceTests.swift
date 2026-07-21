import XCTest
@testable import Goldcomb

/// SavedAgent persistence after role + persona were unified into one free-text
/// `role`. Old files (a display `role`, a `personaRole`, or a pre-display-role
/// `role`-means-persona) must migrate into the single field.
final class SavedAgentPersistenceTests: XCTestCase {

    private func decode(_ json: String) throws -> SavedAgent {
        try JSONDecoder().decode(SavedAgent.self, from: Data(json.utf8))
    }

    func testPreDisplayRoleFileMigratesRoleAsIs() throws {
        // Oldest format: `role` alone meant the persona.
        let s = try decode("""
        {"id":"\(UUID().uuidString)","name":"Legacy","directory":"/tmp","role":"planner"}
        """)
        XCTAssertEqual(s.role, "planner")
    }

    func testDisplayRoleWinsOverPersona() throws {
        // Two-field era: the free-text display role is the meaningful one.
        let s = try decode("""
        {"id":"\(UUID().uuidString)","name":"Ada","directory":"/tmp",
         "role":"Tech Lead","personaRole":"advisor"}
        """)
        XCTAssertEqual(s.role, "Tech Lead")
    }

    func testPersonaMigratesWhenNoDisplayRole() throws {
        // Persona-only agent (planner/advisor) → that becomes the role, so its
        // rich built-in persona still resolves in the CLI.
        let s = try decode("""
        {"id":"\(UUID().uuidString)","name":"P","directory":"/tmp",
         "role":"","personaRole":"advisor"}
        """)
        XCTAssertEqual(s.role, "advisor")
    }

    func testWorkerPersonaMapsToNoRole() throws {
        // "worker" was the no-op default persona — it maps to an empty role.
        let s = try decode("""
        {"id":"\(UUID().uuidString)","name":"W","directory":"/tmp",
         "personaRole":"worker"}
        """)
        XCTAssertEqual(s.role, "")
    }

    func testRoundTripsWithoutPersona() throws {
        let original = SavedAgent(
            id: UUID(), name: "Ada", directory: "/tmp", sudo: true,
            role: "Backend engineer", description: "Owns the API")
        let data = try JSONEncoder().encode(original)
        let decoded = try JSONDecoder().decode(SavedAgent.self, from: data)

        XCTAssertEqual(decoded.id, original.id)
        XCTAssertEqual(decoded.role, "Backend engineer")
        XCTAssertEqual(decoded.description, "Owns the API")
        XCTAssertTrue(decoded.sudo)
        // The encoded form no longer carries personaRole.
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertNil(obj?["personaRole"])
    }
}
