import AppKit
import SwiftUI

/// When run as a bare executable (swift run) rather than from an .app bundle,
/// macOS doesn't activate the app, so no window can become key and keyboard
/// input is dead. Forcing the activation policy + activation fixes that.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
}

@main
struct GoldcombApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = SessionStore()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(store)
                .tint(Comb.gold)
                .frame(minWidth: 760, minHeight: 480)
        }
        Settings {
            SettingsView()
        }
    }
}

struct SettingsView: View {
    @State private var command = AppSettings.command

    var body: some View {
        Form {
            Section {
                TextField("Agent command", text: $command)
                    .font(.system(.body, design: .monospaced))
                    .onSubmit { AppSettings.command = command }
                Text("Executable + arguments used to start each agent; --serve is appended automatically. Applies to newly created agents.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Reset to default") {
                    command = AppSettings.defaultCommand
                    AppSettings.command = command
                }
            }
        }
        .padding(20)
        .frame(width: 520)
        .onDisappear { AppSettings.command = command }
    }
}
