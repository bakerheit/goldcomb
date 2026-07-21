import QuickLook
import SwiftUI

/// Shared staged-attachment chip tray (NEXA-112), used by both the user↔agent
/// composer (ChatView) and the room composer (ChatRoomView).
///
/// Each chip shows a live Finder thumbnail, the file name and size; clicking
/// previews via QuickLook, right-click reveals in Finder, and ✕ removes the
/// file from the staged list. The caller drives insertion (picker, drop,
/// paste) via `add(_:)`.
struct AttachmentChipTray: View {
    @Binding var urls: [URL]
    /// Capsule background behind each chip — the chat stacks differ here.
    var tint: Color = Color(nsColor: .quaternaryLabelColor)
    /// Opens the QuickLook panel; falls back to the enclosing
    /// `AttachmentDropSurface`'s panel, then to NSWorkspace.
    var onPreview: ((URL) -> Void)?

    @Environment(\.previewAttachment) private var envPreview

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(urls, id: \.self) { url in
                    chip(url)
                }
            }
        }
    }

    private func chip(_ url: URL) -> some View {
        HStack(spacing: 5) {
            FileThumbnailView(url: url, size: 18)
            VStack(alignment: .leading, spacing: 0) {
                Text(url.lastPathComponent)
                    .font(.caption)
                    .lineLimit(1)
                Text(byteLabel(url))
                    .font(.system(size: 9))
                    .foregroundStyle(.tertiary)
            }
            Button {
                urls.removeAll { $0 == url }
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            .buttonStyle(.plain)
            .help("Remove")
        }
        .padding(.horizontal, 8).padding(.vertical, 4)
        .background(tint.opacity(0.22), in: Capsule())
        .contentShape(Capsule())
        .onTapGesture {
            if let preview = onPreview ?? envPreview {
                preview(url)
            } else {
                NSWorkspace.shared.open(url)
            }
        }
        .help("Click to preview")
        .contextMenu {
            Button("Reveal in Finder") {
                NSWorkspace.shared.activateFileViewerSelecting([url])
            }
        }
    }

    private func byteLabel(_ url: URL) -> String {
        let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        return ByteCountFormatter.string(fromByteCount: Int64(size),
                                         countStyle: .file)
    }
}

/// A file's Finder thumbnail (icon or content preview), asynchronously loaded.
struct FileThumbnailView: View {
    let url: URL
    var size: CGFloat = 18
    @State private var image: NSImage?

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
            } else {
                Image(systemName: "doc")
            }
        }
        .frame(width: size, height: size)
        .task(id: url) {
            let loaded = NSWorkspace.shared.icon(forFile: url.path)
            loaded.size = NSSize(width: size * 2, height: size * 2)
            image = loaded
        }
    }
}

/// Self-contained QuickLook + drag-drop wrapper for attachment picking.
/// Prefer the `inAttachmentDropSurface` modifier (it keeps the caller's
/// layout untouched); the wrapper view below stays for explicit composition.
/// The surface supplies the QuickLook panel binding and the drop highlight,
/// so neither stack re-implements them; an `AttachmentChipTray` inside picks
/// the panel up through the `previewAttachment` environment.
struct AttachmentDropSurface<Content: View>: View {
    @Binding var urls: [URL]
    @ViewBuilder var content: () -> Content

    @State private var dropTargeted = false
    @State private var preview: URL?

    var body: some View {
        content()
            .environment(\.previewAttachment, { preview = $0 })
            .quickLookPreview($preview)
            .onDrop(of: [.fileURL], isTargeted: $dropTargeted) { providers in
                for provider in providers {
                    _ = provider.loadObject(ofClass: URL.self) { url, _ in
                        guard let url else { return }
                        DispatchQueue.main.async {
                            if !urls.contains(url) { urls.append(url) }
                        }
                    }
                }
                return true
            }
            .background(dropTargeted ? Comb.gold.opacity(0.08) : Color.clear)
    }
}

extension View {
    /// Drop a `AttachmentChipTray` into this view and call this once on the
    /// enclosing container: files dropped anywhere on it are staged in `urls`,
    /// and chip clicks open QuickLook (NEXA-112).
    func inAttachmentDropSurface(urls: Binding<[URL]>) -> some View {
        AttachmentDropSurface(urls: urls) { self }
    }
}

/// Hands a tray chip's click up to the enclosing `AttachmentDropSurface`'s
/// QuickLook binding (avoids every caller wiring its own panel).
private struct PreviewAttachmentKey: EnvironmentKey {
    static let defaultValue: ((URL) -> Void)? = nil
}

extension EnvironmentValues {
    var previewAttachment: ((URL) -> Void)? {
        get { self[PreviewAttachmentKey.self] }
        set { self[PreviewAttachmentKey.self] = newValue }
    }
}
