import XCTest
@testable import Goldcomb

final class SavedAgentPersistenceTests: XCTestCase {
    func testLegacyRoleDecodesAsPersonaWithoutDisplayRole() throws {
        let id = UUID()
        let json = """
        {"id":"\(id.uuidString)","name":"Legacy","directory":"/tmp","role":"planner"}
        """
        let saved = try JSONDecoder().decode(SavedAgent.self, from: Data(json.utf8))

        XCTAssertEqual(saved.personaRole, "planner")
        XCTAssertEqual(saved.role, "")
        XCTAssertEqual(saved.description, "")
    }

    func testDisplayMetadataAndPersonaRoundTripIndependently() throws {
        let original = SavedAgent(
            id: UUID(), name: "Ada", directory: "/tmp", sudo: true,
            role: "Tech Lead", description: "Owns release quality",
            personaRole: "advisor"
        )
        let decoded = try JSONDecoder().decode(
            SavedAgent.self, from: JSONEncoder().encode(original)
        )

        XCTAssertEqual(decoded.id, original.id)
        XCTAssertEqual(decoded.role, "Tech Lead")
        XCTAssertEqual(decoded.description, "Owns release quality")
        XCTAssertEqual(decoded.personaRole, "advisor")
        XCTAssertTrue(decoded.sudo)
    }
}
