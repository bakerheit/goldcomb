import SwiftUI

/// The composer both chat stacks share (NEXA-110): staged-attachment tray,
/// text field, paperclip, and send (or stop, while a turn runs), with
/// per-mode accessory slots so ChatView and ChatRoomView keep their own
/// affordances without drifting on the shared 70%.
///
/// Layout (one vertical stack, spacing 6, padded 10):
///
///     [accessory]      ← mode popup strip: slash palette (CV) / mention
///                        popup + tag hint (CRV); EmptyView when idle
///     [chip tray]      ← shared AttachmentChipTray, shown when staged ≠ ∅
///     (paperclip) [leading] [field] [trailing] (send/stop)
///
/// Slots:
/// - `leading` / `trailing`: buttons flanking the field in the input row.
///   CV passes EmptyView (its utility strip — command menu, model chip, sudo,
///   new-conversation — stays a ChatView row below the composer); CRV passes
///   its mention menu leading.
/// - `accessory`: the popup strip above the tray. CV: slash-command palette;
///   CRV: mention autocomplete / tag hint. Intentionally per-mode.
///
/// The caller owns the draft and the staged URLs; the composer reports taps
/// through `onSubmit` (Return in the field), `onSend`, `onStop`, and
/// `onAttach` (paperclip — the caller presents its own importer, since the
/// room's stages differently from the agent chat's). The attachment drop
/// surface also stays with the caller: the room's QuickLook routing and the
/// agent chat's cover slightly different enclosing views.
struct SharedComposerView<Leading: View, Trailing: View, Accessory: View>: View {
    @Binding var draft: String
    @Binding var staged: [URL]
    var placeholder: String = "Message…"
    /// Capsule tint for the staged chips (the agent chat tints gold).
    var trayTint: Color = Color(nsColor: .quaternaryLabelColor)
    /// Field growth limit (1…N lines).
    var lineLimit: ClosedRange<Int> = 1...5
    /// Show the stop button instead of send (a turn is running).
    var showsStop = false
    /// The composed send/stop button's diameter: 28 in the agent chat, 24
    /// (title3) in rooms.
    var buttonSize: CGFloat = 24
    var canSend = false
    var onSubmit: () -> Void = {}
    var onSend: () -> Void = {}
    var onStop: () -> Void = {}
    var onAttach: () -> Void = {}

    @ViewBuilder var leading: Leading
    @ViewBuilder var trailing: Trailing
    @ViewBuilder var accessory: Accessory

    var body: some View {
        VStack(spacing: 6) {
            accessory
            if !staged.isEmpty {
                AttachmentChipTray(urls: $staged, tint: trayTint)
            }
            HStack(spacing: 8) {
                attachButton
                leading
                TextField(placeholder, text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(lineLimit)
                    .onSubmit(onSubmit)
                trailing
                sendStop
            }
        }
        .padding(10)
    }

    private var attachButton: some View {
        Button(action: onAttach) {
            Image(systemName: "paperclip")
                .font(.body)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Attach files (or drop them here)")
    }

    @ViewBuilder
    private var sendStop: some View {
        if showsStop {
            Button(action: onStop) {
                Image(systemName: "stop.circle.fill")
                    .font(.system(size: buttonSize))
                    .foregroundStyle(.red)
                    .frame(width: buttonSize + 8, height: buttonSize + 8)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Stop this turn")
        } else {
            Button(action: onSend) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: buttonSize))
                    .foregroundStyle(canSend ? AnyShapeStyle(Comb.gold)
                                             : AnyShapeStyle(.tertiary))
                    .frame(width: buttonSize + 8, height: buttonSize + 8)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(!canSend)
            .help("Send (Return)")
        }
    }
}
