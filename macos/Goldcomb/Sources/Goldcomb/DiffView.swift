import SwiftUI

/// The changed-file diff sheet (NEXA-99): tapping a file in the Project
/// header's changed-files popover opens this. Requests the file's diff from
/// the serve process on appear and on a staged/unstaged flip, and renders
/// the reply (or its error) inline.
struct DiffView: View {
    @ObservedObject var session: AgentSession
    /// The tapped file — repo-relative path plus which porcelain bucket it
    /// came from, which seeds the staged/unstaged toggle.
    let file: GitStatus.File

    @Environment(\.dismiss) private var dismiss
    /// nil until the user flips the toggle — the file's bucket is the
    /// initial selection, so no @State seeding (and no staleness) is needed.
    @State private var selection: Bool? = nil

    private var staged: Bool {
        selection ?? (file.status == .staged)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            Divider()
            content
        }
        .padding(16)
        .frame(minWidth: 560, minHeight: 420)
        .onAppear { request() }
        .onChange(of: staged) { _ in request() }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Text(file.path)
                .font(.system(.callout, design: .monospaced).weight(.semibold))
                .lineLimit(1)
                .truncationMode(.middle)
            // Untracked files have no diff yet (the server replies with a
            // notice), so the staged/unstaged choice is meaningless for them.
            if file.status != .untracked {
                Picker("", selection: Binding(
                    get: { staged },
                    set: { selection = $0 }
                )) {
                    Text("Unstaged").tag(false)
                    Text("Staged").tag(true)
                }
                .pickerStyle(.segmented)
                .fixedSize()
            }
            Spacer()
            Button("Done") { dismiss() }
                .keyboardShortcut(.cancelAction)
        }
    }

    @ViewBuilder
    private var content: some View {
        if let error = session.gitDiffError {
            Label(error, systemImage: "exclamationmark.triangle")
                .font(.callout)
                .foregroundStyle(.red)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
        } else if let diff = session.gitDiff, diff.path == file.path {
            DiffText(diff: diff.diff, truncated: diff.truncated)
        } else {
            // Request in flight (or a stale reply for another file — the 2s
            // header poll can interleave). Loading beats showing the wrong
            // file's diff.
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Loading diff…")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
        }
    }

    private func request() {
        session.requestGitDiff(path: file.path, staged: staged)
    }
}
