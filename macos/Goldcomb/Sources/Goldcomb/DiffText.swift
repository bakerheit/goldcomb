import Foundation
import SwiftUI

/// One classified line of a unified diff (NEXA-99). Line-oriented and pure —
/// no syntax highlighting, just the five structural classes a `git diff`
/// emits plus a passthrough for payloads that are not diffs at all (the
/// server's untracked-file notice renders as plain text).
struct DiffLine: Equatable, Identifiable {
    enum Kind: Equatable {
        /// ---/+++ file headers, plus diff/index lines (rendered dimmer).
        case fileHeader
        /// @@ -a,b +c,d @@ hunk markers.
        case hunk
        /// +added line.
        case add
        /// -removed line.
        case del
        /// everything else: " context", "\ No newline", and non-diff text
        /// (the untracked notice) — plain, untinted.
        case context
    }

    let kind: Kind
    let text: String
    /// Stable within one parsed diff — the line's position.
    var id: Int { offset }
    let offset: Int
}

/// Pure line-oriented unified-diff parser. First-match-wins classification;
/// the only subtlety is that a leading "---"/"+++" is a file header while a
/// single leading "-"/"+" is a del/add line, so the longer prefix is checked
/// first.
enum DiffParse {
    static func parse(_ source: String) -> [DiffLine] {
        source.components(separatedBy: "\n").enumerated().map { offset, line in
            DiffLine(kind: classify(line), text: line, offset: offset)
        }
    }

    static func classify(_ line: String) -> DiffLine.Kind {
        if line.hasPrefix("---") || line.hasPrefix("+++")
            || line.hasPrefix("diff ") || line.hasPrefix("index ") {
            return .fileHeader
        }
        if line.hasPrefix("@@") { return .hunk }
        if line.hasPrefix("+") { return .add }
        if line.hasPrefix("-") { return .del }
        return .context
    }
}

/// The reply payload of a `git_diff` command (NEXA-99): the file's unified
/// diff, or the untracked notice in the same `diff` field. Decoded from the
/// serve protocol's `git_diff` event, mirroring GitStatus.decode's style —
/// nil on a malformed payload, never a crash.
struct GitDiff: Equatable {
    let path: String
    let staged: Bool
    let diff: String
    let truncated: Bool

    static func decode(event: [String: Any]) -> GitDiff? {
        guard let path = event["path"] as? String, !path.isEmpty,
              let diff = event["diff"] as? String
        else { return nil }
        return GitDiff(
            path: path,
            staged: event["staged"] as? Bool ?? false,
            diff: diff,
            truncated: event["truncated"] as? Bool ?? false
        )
    }
}

/// Renders a unified diff as tinted, monospaced lines in the same chrome as
/// MarkdownText's `.code` block (mono font, quaternary background, rounded
/// box). Green adds, red dels, amber hunks, dimmed file headers — the app's
/// existing accent palette; no syntax highlighting.
struct DiffText: View {
    let diff: String
    /// Server-side truncation flag — a "diff truncated" footer is appended
    /// when the payload was clipped.
    var truncated: Bool = false

    private var lines: [DiffLine] { DiffParse.parse(diff) }

    var body: some View {
        ScrollView([.horizontal, .vertical]) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(lines) { line in
                    Text(line.text.isEmpty ? " " : line.text)
                        .foregroundStyle(tint(for: line.kind))
                }
                if truncated {
                    Text("… diff truncated")
                        .foregroundStyle(.secondary)
                        .italic()
                        .padding(.top, 4)
                }
            }
            .font(.system(.caption, design: .monospaced))
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(8)
        }
        .background(.quaternary.opacity(0.5),
                    in: RoundedRectangle(cornerRadius: 6))
        .textSelection(.enabled)
    }

    private func tint(for kind: DiffLine.Kind) -> Color {
        switch kind {
        case .add: return .green
        case .del: return .red
        case .hunk: return Comb.amber
        case .fileHeader: return .secondary
        case .context: return .primary
        }
    }
}
