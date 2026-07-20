import Foundation

/// One row in a session's transcript, mirroring the CLI's static transcript:
/// user prompts, streamed assistant messages, tool bullets and their results.
struct TranscriptItem: Identifiable {
    enum Kind {
        case user
        case assistant
        case toolCall
        case toolResult
        case nudge
        case log     // stderr lines from the agent process
        case error
    }

    let id = UUID()
    let kind: Kind
    var text: String
}
