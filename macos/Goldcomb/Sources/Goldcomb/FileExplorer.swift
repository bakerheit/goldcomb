import Foundation
import SwiftUI

/// One entry in the project file tree. Children are loaded lazily the first
/// time a directory is expanded — large repos never pay for a full scan.
final class FileNode: Identifiable {
    let url: URL
    let name: String
    let isDirectory: Bool
    var children: [FileNode]?

    var id: URL { url }

    init(url: URL, name: String, isDirectory: Bool, children: [FileNode]? = nil) {
        self.url = url
        self.name = name
        self.isDirectory = isDirectory
        self.children = children
    }
}

/// Owns the file tree shown in the right-side explorer for one project
/// folder. Reading is sync-but-lazy (only expanded directories hit disk);
/// `refresh()` re-reads expanded ones in place so the tree picks up changes
/// (e.g. files an agent just wrote) without collapsing.
final class FileExplorerModel: ObservableObject {
    @Published var root: FileNode

    /// Prefixes skipped entirely — noise or huge trees.
    private static let hiddenPrefixes: [String] = [
        ".git", ".svn", ".hg",
        ".venv", "venv", "env",
        "node_modules",
        "__pycache__",
        ".build", "DerivedData",
        ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".next", ".nuxt", ".turbo",
        "dist", "build",
        ".idea", ".vscode",
    ]

    init(directory: URL) {
        self.root = FileNode(
            url: directory,
            name: directory.lastPathComponent,
            isDirectory: true
        )
        root.children = Self.loadChildren(of: directory)
    }

    /// Re-read every directory currently expanded, preserving expansion.
    func refresh() {
        refreshNode(root)
        objectWillChange.send()
    }

    private func refreshNode(_ node: FileNode) {
        guard node.isDirectory, node.children != nil else { return }
        node.children = Self.loadChildren(of: node.url)
        node.children?.filter(\.isDirectory).forEach(refreshNode)
    }

    /// Expand a directory row on demand (called from the view's onAppear).
    func loadChildren(of node: FileNode) {
        guard node.isDirectory, node.children == nil else { return }
        node.children = Self.loadChildren(of: node.url)
        objectWillChange.send()
    }

    static func loadChildren(of directory: URL) -> [FileNode] {
        guard let urls = try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: []
        ) else { return [] }
        var dirs: [FileNode] = []
        var files: [FileNode] = []
        for url in urls {
            let name = url.lastPathComponent
            if name.hasPrefix(".") || hiddenPrefixes.contains(name) { continue }
            let isDir = (try? url.resourceValues(forKeys: [.isDirectoryKey]))?
                .isDirectory ?? false
            let node = FileNode(url: url, name: name, isDirectory: isDir)
            if isDir { dirs.append(node) } else { files.append(node) }
        }
        let byName: (FileNode, FileNode) -> Bool = {
            $0.name.localizedStandardCompare($1.name) == .orderedAscending
        }
        return dirs.sorted(by: byName) + files.sorted(by: byName)
    }
}

/// SF Symbol for a file row, chosen by extension.
func iconForFile(_ name: String) -> String {
    switch (name as NSString).pathExtension.lowercased() {
    case "swift", "py", "js", "ts", "tsx", "jsx", "go", "rs", "c", "h", "cpp",
         "java", "kt", "rb", "php", "sh", "m", "mm":
        return "chevron.left.forwardslash.chevron.right"
    case "md", "txt", "rst":
        return "doc.plaintext"
    case "json", "yaml", "yml", "toml", "xml", "plist":
        return "curlybraces"
    case "png", "jpg", "jpeg", "gif", "svg", "webp", "heic", "ico":
        return "photo"
    case "html", "css", "scss":
        return "globe"
    case "pdf":
        return "doc.richtext"
    case "zip", "tar", "gz", "xz", "7z", "dmg":
        return "doc.zipper"
    case "mp3", "wav", "m4a", "flac", "ogg":
        return "waveform"
    case "mp4", "mov", "mkv", "webm":
        return "film"
    case "db", "sqlite", "sqlite3":
        return "cylinder"
    case "" where name.hasPrefix("Dockerfile"):
        return "shippingbox"
    default:
        return "doc"
    }
}

/// Right-side panel: the active project's files as an expandable tree.
/// Selecting a file opens it in the default editor; the context menu offers
/// Finder/Terminal actions. A refresh button re-reads expanded folders —
/// also triggered automatically when an agent finishes a turn.
struct FileExplorerView: View {
    @ObservedObject var model: FileExplorerModel

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "folder.fill")
                    .foregroundStyle(.secondary)
                Text(model.root.name)
                    .font(.headline)
                    .lineLimit(1)
                Spacer()
                Button {
                    model.refresh()
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("Refresh (agents may have changed files)")
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            Divider()
            // Root's children sit at the top level; the header above already
            // names the project folder.
            List(model.root.children ?? [], children: \.children) { node in
                FileNodeRow(node: node, model: model)
            }
            .listStyle(.sidebar)
        }
        .frame(minWidth: 180)
    }
}

struct FileNodeRow: View {
    let node: FileNode
    @ObservedObject var model: FileExplorerModel

    var body: some View {
        Label {
            Text(node.name)
                .lineLimit(1)
                .truncationMode(.middle)
        } icon: {
            Image(systemName: node.isDirectory ? "folder" : iconForFile(node.name))
                .foregroundStyle(node.isDirectory ? AnyShapeStyle(.tint)
                                                  : AnyShapeStyle(.secondary))
        }
        .onAppear { model.loadChildren(of: node) }
        .onTapGesture {
            if !node.isDirectory {
                NSWorkspace.shared.open(node.url)
            }
        }
        .contextMenu {
            Button("Open") { NSWorkspace.shared.open(node.url) }
            Button("Show in Finder") {
                NSWorkspace.shared.activateFileViewerSelecting([node.url])
            }
            if node.isDirectory {
                Button("Open in Terminal") {
                    NSWorkspace.shared.openApplication(
                        at: URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app"),
                        configuration: {
                            let c = NSWorkspace.OpenConfiguration()
                            c.arguments = [node.url.path]
                            return c
                        }()
                    )
                }
            }
            Divider()
            Button("Copy Path") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(node.url.path, forType: .string)
            }
        }
    }
}
