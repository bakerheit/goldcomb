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
        .onAppear { model.refresh() }
        .sheet(item: Binding(get: { draft.map(DraftBox.init) }, set: { draft = $0?.value })) { box in
            ProviderEditor(draft: box.value) { model.save($0); draft = nil }
        }
    }
}

private struct DraftBox: Identifiable { let id = UUID(); var value: ProviderDraft }

private struct ProviderEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State var draft: ProviderDraft
    let save: (ProviderDraft) -> Void
    private let types = ["anthropic", "openai", "gemini", "openai-compatible"]
    var body: some View {
        Form {
            TextField("Name", text: $draft.name)
            Picker("Type", selection: $draft.type) { ForEach(types, id: \.self) { Text($0) } }
            SecureField(draft.originalName == nil ? "API key" : "New API key (leave blank to keep)",
                        text: $draft.apiKey)
            TextField("Base URL", text: $draft.baseURL)
            TextField("Default model", text: $draft.defaultModel)
            Toggle("Make current", isOn: $draft.makeCurrent)
            HStack { Spacer(); Button("Cancel") { dismiss() }; Button("Save") { save(draft) }
                .buttonStyle(.borderedProminent).disabled(draft.name.isEmpty) }
        }.padding(20).frame(width: 500)
    }
}
