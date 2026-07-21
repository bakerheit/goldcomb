import Foundation
import SwiftUI

struct ManagedProvider: Codable, Identifiable, Equatable {
    let name: String
    let type: String
    let base_url: String
    let default_model: String
    let has_key: Bool
    let key_source: String
    var id: String { name }
}

struct ProviderListing: Codable {
    let config_revision: Int
    let current: [String: String]
    let providers: [ManagedProvider]
}

/// A known-provider preset (from `goldcomb config presets --json`, backed by
/// goldcomb/presets.py). Fills in everything but the key so "Add provider" is
/// pick-a-name-and-paste-a-key rather than a form the user has to know the
/// base URL and default model for.
struct ProviderPreset: Codable, Identifiable, Equatable {
    let key: String
    let label: String
    let type: String
    let default_model: String
    let base_url: String
    /// The endpoint the adapter uses with no override (empty for
    /// openai-compatible, which requires one). Shown as a placeholder hint.
    let default_base_url: String
    /// Whether a base URL is mandatory (openai-compatible) vs. an optional
    /// override (anthropic / openai / gemini have a baked-in endpoint).
    let requires_base_url: Bool
    let env: String
    let env_present: Bool
    let key_url: String
    let needs_key: Bool
    let note: String
    var id: String { key }
}

private struct PresetListing: Codable { let presets: [ProviderPreset] }

extension ProviderDraft {
    /// Prefill from a preset, so adding a known provider only needs a key.
    /// Leaves the name if the user already typed one. Pure so it's testable
    /// without the SwiftUI editor.
    mutating func apply(_ p: ProviderPreset) {
        type = p.type
        baseURL = p.base_url
        defaultModel = p.default_model
        if name.isEmpty { name = p.key }
    }
}

struct ProviderDraft: Equatable {
    var originalName: String? = nil
    var name = ""
    var type = "anthropic"
    var apiKey = ""
    var baseURL = ""
    var defaultModel = ""
    var makeCurrent = false
}

final class ProviderSettingsModel: ObservableObject {
    @Published var providers: [ManagedProvider] = []
    @Published var current = ""
    @Published var revision = 0
    @Published var error: String?
    @Published var isBusy = false
    /// Known-provider presets, fetched once. Empty until loaded (the editor
    /// falls back to manual entry, so a fetch failure is never fatal).
    @Published var presets: [ProviderPreset] = []

    static func commandArguments(action: String, draft: ProviderDraft? = nil) -> [String] {
        var args = ["config", action, "--json"]
        guard let draft else { return args }
        args += ["--name", draft.originalName ?? draft.name]
        if action == "add" || !draft.type.isEmpty { args += ["--type", draft.type] }
        if let original = draft.originalName, original != draft.name {
            args += ["--new-name", draft.name]
        }
        args += ["--base-url", draft.baseURL, "--default-model", draft.defaultModel]
        if !draft.apiKey.isEmpty { args.append("--api-key-stdin") }
        if draft.makeCurrent { args.append("--current") }
        return args
    }

    func refresh() { run(action: "list", draft: nil) }
    func save(_ draft: ProviderDraft) {
        run(action: draft.originalName == nil ? "add" : "update", draft: draft)
    }

    /// Fetch the known-provider presets once (idempotent — no-op if loaded).
    func loadPresets() {
        guard presets.isEmpty else { return }
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let invocation = try AppSettings.commandInvocation()
                let process = Process(), output = Pipe()
                process.executableURL = URL(fileURLWithPath: invocation.executable)
                process.arguments = invocation.arguments + ["config", "presets", "--json"]
                process.standardOutput = output
                process.standardError = Pipe()
                try process.run()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let listing = try JSONDecoder().decode(PresetListing.self, from: data)
                DispatchQueue.main.async { self.presets = listing.presets }
            } catch {
                // Non-fatal: the editor still allows manual entry.
            }
        }
    }

    private func run(action: String, draft: ProviderDraft?) {
        isBusy = true; error = nil
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let invocation = try AppSettings.commandInvocation()
                let process = Process(), output = Pipe(), input = Pipe()
                process.executableURL = URL(fileURLWithPath: invocation.executable)
                process.arguments = invocation.arguments + Self.commandArguments(action: action,
                                                                                  draft: draft)
                process.standardOutput = output
                process.standardError = Pipe()
                process.standardInput = input
                try process.run()
                if let key = draft?.apiKey, !key.isEmpty {
                    input.fileHandleForWriting.write(Data(key.utf8))
                }
                input.fileHandleForWriting.closeFile()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let listing = try JSONDecoder().decode(ProviderListing.self, from: data)
                DispatchQueue.main.async {
                    self.providers = listing.providers
                    self.current = listing.current["provider"] ?? ""
                    self.revision = listing.config_revision
                    self.isBusy = false
                    NotificationCenter.default.post(name: .configRevisionChanged,
                                                    object: listing.config_revision)
                }
            } catch {
                DispatchQueue.main.async { self.error = error.localizedDescription; self.isBusy = false }
            }
        }
    }
}

extension Notification.Name {
    static let configRevisionChanged = Notification.Name("configRevisionChanged")
}

struct ProvidersSettingsView: View {
    @StateObject private var model = ProviderSettingsModel()
    @State private var draft: ProviderDraft?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text("Providers").font(.headline); Spacer(); Button("Add") { draft = ProviderDraft() } }
            if let error = model.error { Text(error).foregroundStyle(.red).font(.caption) }
            List(model.providers) { provider in
                Button { draft = ProviderDraft(originalName: provider.name, name: provider.name,
                    type: provider.type, baseURL: provider.base_url,
                    defaultModel: provider.default_model, makeCurrent: provider.name == model.current) } label: {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(provider.name + (provider.name == model.current ? "  Current" : ""))
                            Text("\(provider.type) · key: \(provider.key_source)")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer(); Image(systemName: "chevron.right").foregroundStyle(.secondary)
                    }
                }.buttonStyle(.plain)
            }
        }
        .onAppear { model.refresh(); model.loadPresets() }
        .sheet(item: Binding(get: { draft.map(DraftBox.init) }, set: { draft = $0?.value })) { box in
            ProviderEditor(draft: box.value, presets: model.presets) { model.save($0); draft = nil }
        }
    }
}

private struct DraftBox: Identifiable { let id = UUID(); var value: ProviderDraft }

private struct ProviderEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State var draft: ProviderDraft
    let presets: [ProviderPreset]
    let save: (ProviderDraft) -> Void

    /// The preset currently applied (new providers only), tracked so the
    /// key-hint and "get a key" link can reflect it.
    @State private var chosen: ProviderPreset?
    private let types = ["anthropic", "openai", "gemini", "openai-compatible"]
    private var isNew: Bool { draft.originalName == nil }

    var body: some View {
        Form {
            // For a NEW provider, lead with the preset picker: choosing one
            // fills in type / base URL / default model, so all that's left is
            // the key. Editing an existing provider skips this.
            if isNew && !presets.isEmpty {
                Picker("Provider", selection: presetSelection) {
                    Text("Choose a provider…").tag(String?.none)
                    ForEach(presets) { p in Text(p.label).tag(String?.some(p.key)) }
                    Text("Custom / other").tag(String?.some("__custom__"))
                }
                if let note = chosen?.note, !note.isEmpty {
                    Text(note).font(.caption).foregroundStyle(.secondary)
                }
            }

            TextField("Name", text: $draft.name)
            Picker("Type", selection: $draft.type) { ForEach(types, id: \.self) { Text($0) } }

            keyField

            TextField("Default model", text: $draft.defaultModel)
            baseURLField
            Toggle("Make current", isOn: $draft.makeCurrent)
            HStack { Spacer(); Button("Cancel") { dismiss() }; Button("Save") { save(draft) }
                .buttonStyle(.borderedProminent).disabled(!canSave) }
        }.padding(20).frame(width: 500)
    }

    /// openai-compatible endpoints have no built-in URL — the adapter fails
    /// without one — so the field is required and always shown. The first-party
    /// types (anthropic / openai / gemini) have a baked-in endpoint, so the
    /// field is an optional override tucked under "Advanced" rather than a
    /// blank box that looks unfinished.
    private var requiresBaseURL: Bool { draft.type == "openai-compatible" }

    /// The endpoint used when Base URL is left blank, for the placeholder hint.
    private var builtinEndpoint: String { chosen?.default_base_url ?? "" }

    private var canSave: Bool {
        guard !draft.name.isEmpty else { return false }
        // Don't let the user create a compatible provider with no endpoint —
        // it would only fail at first use.
        if requiresBaseURL && draft.baseURL.trimmingCharacters(in: .whitespaces).isEmpty {
            return false
        }
        return true
    }

    @ViewBuilder
    private var baseURLField: some View {
        if requiresBaseURL {
            TextField("Base URL (required)", text: $draft.baseURL)
        } else {
            DisclosureGroup("Advanced") {
                TextField(
                    builtinEndpoint.isEmpty ? "Base URL (optional)" : builtinEndpoint,
                    text: $draft.baseURL)
                Text(builtinEndpoint.isEmpty
                     ? "Optional — overrides the built-in endpoint."
                     : "Optional — leave blank to use \(builtinEndpoint).")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var keyField: some View {
        // A preset whose key is already in the environment: the CLI picks it up
        // on add, so the field can be left blank.
        if let p = chosen, p.env_present {
            SecureField("API key (found \(p.env) in your environment — optional)",
                        text: $draft.apiKey)
        } else if let p = chosen, !p.needs_key {
            SecureField("API key (not required for \(p.label))", text: $draft.apiKey)
        } else {
            SecureField(isNew ? "API key" : "New API key (leave blank to keep)",
                        text: $draft.apiKey)
        }
        if let p = chosen, !p.key_url.isEmpty, let url = URL(string: p.key_url) {
            Link("Get an API key ↗", destination: url).font(.caption)
        }
    }

    /// Binding for the preset picker: applying a preset prefills the draft.
    private var presetSelection: Binding<String?> {
        Binding(
            get: { chosen?.key },
            set: { key in
                guard let key else { chosen = nil; return }
                if key == "__custom__" {
                    chosen = nil
                    draft.type = "openai-compatible"
                    return
                }
                guard let p = presets.first(where: { $0.key == key }) else { return }
                chosen = p
                draft.apply(p)
            })
    }
}
