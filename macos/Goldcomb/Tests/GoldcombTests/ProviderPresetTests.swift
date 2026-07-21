import XCTest
@testable import Goldcomb

/// The "Add provider" prefill: choosing a known provider must fill in type,
/// base URL, and default model so the user only supplies a key. Decoding the
/// CLI's `config presets --json` shape is a contract with goldcomb/presets.py.
final class ProviderPresetTests: XCTestCase {

    private func preset(_ key: String, type: String, base: String = "",
                        model: String = "", env: String = "",
                        envPresent: Bool = false, needsKey: Bool = true,
                        defaultBase: String = "", requiresBase: Bool = false)
        -> ProviderPreset {
        ProviderPreset(key: key, label: key, type: type, default_model: model,
                       base_url: base, default_base_url: defaultBase,
                       requires_base_url: requiresBase, env: env,
                       env_present: envPresent, key_url: "", needs_key: needsKey,
                       note: "")
    }

    func testApplyingAPresetPrefillsTheDraft() {
        var draft = ProviderDraft()
        draft.apply(preset("gemini", type: "gemini", model: "gemini-2.5-flash"))
        XCTAssertEqual(draft.type, "gemini")
        XCTAssertEqual(draft.defaultModel, "gemini-2.5-flash")
        XCTAssertEqual(draft.name, "gemini")  // filled from the key
    }

    func testApplyPrefillsBaseURLForOpenAICompatible() {
        var draft = ProviderDraft()
        draft.apply(preset("kimi", type: "openai-compatible",
                            base: "https://api.moonshot.ai/v1"))
        XCTAssertEqual(draft.type, "openai-compatible")
        XCTAssertEqual(draft.baseURL, "https://api.moonshot.ai/v1")
    }

    func testApplyDoesNotClobberAUserTypedName() {
        var draft = ProviderDraft()
        draft.name = "my-claude"
        draft.apply(preset("anthropic", type: "anthropic"))
        XCTAssertEqual(draft.name, "my-claude")
    }

    func testBuiltinEndpointTypesLeaveBaseURLBlank() {
        // Applying a first-party preset must not fill Base URL — the endpoint
        // is baked into the type, so the field stays an empty optional.
        var draft = ProviderDraft()
        draft.apply(preset("anthropic", type: "anthropic", base: "",
                            defaultBase: "https://api.anthropic.com"))
        XCTAssertEqual(draft.baseURL, "")
    }

    func testRequiresBaseURLTracksTheType() {
        // The contract the editor keys its required-vs-advanced layout on.
        XCTAssertTrue(preset("k", type: "openai-compatible", requiresBase: true)
                        .requires_base_url)
        XCTAssertFalse(preset("a", type: "anthropic").requires_base_url)
    }

    func testDecodesTheCLIPresetShape() throws {
        // Exactly what `config presets --json` emits for one entry.
        let json = #"""
        {"key":"anthropic","label":"Anthropic — Claude","type":"anthropic",
         "default_model":"claude-opus-4-8","base_url":"",
         "default_base_url":"https://api.anthropic.com","requires_base_url":false,
         "env":"ANTHROPIC_API_KEY","env_present":true,
         "key_url":"https://console.anthropic.com/settings/keys",
         "needs_key":true,"note":""}
        """#
        let p = try JSONDecoder().decode(ProviderPreset.self, from: Data(json.utf8))
        XCTAssertEqual(p.key, "anthropic")
        XCTAssertEqual(p.default_model, "claude-opus-4-8")
        XCTAssertEqual(p.default_base_url, "https://api.anthropic.com")
        XCTAssertFalse(p.requires_base_url)  // built-in endpoint, override optional
        XCTAssertTrue(p.env_present)
        XCTAssertTrue(p.needs_key)
    }
}
