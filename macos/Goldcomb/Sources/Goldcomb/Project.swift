import Foundation

/// A project groups related agents in the sidebar. It's purely an
/// organizational concept: a display name plus the folder its agents
/// run in. The folder's display name is the default project name.
final class Project: ObservableObject, Identifiable {
    /// Stable identity, persisted to disk so agents can be regrouped under
    /// their project on relaunch.
    let id: UUID
    @Published var name: String
    let directory: URL

    init(id: UUID = UUID(), name: String, directory: URL) {
        self.id = id
        self.name = name
        self.directory = directory
    }
}
