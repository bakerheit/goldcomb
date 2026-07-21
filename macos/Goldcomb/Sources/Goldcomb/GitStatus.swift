import Foundation

/// Read-only working-tree state for a project, decoded from the serve
/// protocol's `git_status` event (NEXA-97, slice 1 of the Git-integration
/// epic NEXA-88). Provider-neutral snapshot: branch, ahead/behind counts, and
/// the changed files grouped by porcelain status. Polled on the ProjectView
/// header's existing 2s reload tick — never mutated from the app.
struct GitStatus: Codable, Equatable {
    /// One changed file: repo-relative path plus its porcelain bucket.
    struct File: Codable, Equatable, Identifiable {
        /// staged / unstaged / untracked (the three buckets the backend emits).
        enum Status: String, Codable {
            case staged, unstaged, untracked
        }

        let path: String
        let status: Status

        /// Stable within a snapshot — a file appears once per status bucket.
        var id: String { "\(status.rawValue):\(path)" }
    }

    let branch: String
    let ahead: Int
    let behind: Int
    let files: [File]

    /// Total changed files — the header's dirty badge count. 0 = clean tree.
    var dirtyCount: Int { files.count }
    var isClean: Bool { files.isEmpty }

    var staged: [File] { files.filter { $0.status == .staged } }
    var unstaged: [File] { files.filter { $0.status == .unstaged } }
    var untracked: [File] { files.filter { $0.status == .untracked } }

    /// Decode a `git_status` event payload into a GitStatus. Returns nil when
    /// the required fields are missing/mistyped — a malformed event must not
    /// crash the handler, and the header keeps its last good snapshot (stale
    /// beats absent). The `event` discriminator itself is checked by the
    /// switch at the call site.
    static func decode(event: [String: Any]) -> GitStatus? {
        guard let branch = event["branch"] as? String, !branch.isEmpty else { return nil }
        let ahead = event["ahead"] as? Int ?? 0
        let behind = event["behind"] as? Int ?? 0
        let rawFiles = event["files"] as? [[String: Any]] ?? []
        let files: [File] = rawFiles.compactMap { f in
            guard let path = f["path"] as? String,
                  let raw = f["status"] as? String,
                  let status = File.Status(rawValue: raw)
            else { return nil }
            return File(path: path, status: status)
        }
        return GitStatus(branch: branch, ahead: ahead, behind: behind, files: files)
    }
}
