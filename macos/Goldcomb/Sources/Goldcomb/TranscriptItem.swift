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
    /// When this row happened (NEXA-118): live rows stamp themselves on
    /// creation, hydrated rows carry the thread file's `timestamp`. Drives
    /// the day separators and HH:mm labels in the chat transcript.
    var ts: Date = Date()
}

extension TranscriptItem {
    /// Rebuild the visible transcript from a thread interchange file's text
    /// (.ai/threads/<id>.jsonl): a header line, then one message per line.
    /// Only user/assistant messages with content are shown (tool plumbing
    /// isn't representable there); a message's optional `timestamp` (naive
    /// local ISO8601, threads.py) lands on `ts`, falling back to now.
    static func fromThreadFile(_ text: String) -> [TranscriptItem] {
        var items: [TranscriptItem] = []
        for line in text.split(separator: "\n").dropFirst() {
            guard let obj = (try? JSONSerialization.jsonObject(
                with: Data(line.utf8))) as? [String: Any],
                  let content = obj["content"] as? String, !content.isEmpty
            else { continue }
            let ts = TranscriptTime.parseISO(obj["timestamp"] as? String) ?? Date()
            switch obj["role"] as? String {
            case "user":
                items.append(TranscriptItem(kind: .user, text: content, ts: ts))
            case "assistant":
                items.append(TranscriptItem(kind: .assistant, text: content, ts: ts))
            default: break
            }
        }
        return items
    }
}

/// Day separators and HH:mm timestamps for the chat transcript (NEXA-118),
/// the room scheme (ChatRoomView) ported onto `Date`: the thread file's
/// timestamps are naive LOCAL ISO8601 (threads.py `datetime.now().isoformat()`),
/// so parsing in the current timezone is exact, not a convenience.
enum TranscriptTime {
    /// Formatters are cached once rather than built per row: DateFormatter
    /// construction is the expensive part of these labels.
    private static let iso: DateFormatter = {
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.timeZone = .current
        fmt.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
        return fmt
    }()

    private static let isoWholeSeconds: DateFormatter = {
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.timeZone = .current
        fmt.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return fmt
    }()

    private static let dayName: DateFormatter = {
        let fmt = DateFormatter()
        fmt.dateFormat = "EEEE, MMM d"
        return fmt
    }()

    private static let timeOfDay: DateFormatter = {
        let fmt = DateFormatter()
        fmt.dateFormat = "HH:mm"
        return fmt
    }()

    private static let dayAndTime: DateFormatter = {
        let fmt = DateFormatter()
        fmt.dateFormat = "MMM d, HH:mm"
        return fmt
    }()

    /// Parse a naive local ISO8601 timestamp, with or without fractional
    /// seconds; nil on anything else (malformed lines keep hydrating).
    static func parseISO(_ string: String?) -> Date? {
        guard let string else { return nil }
        return iso.date(from: string) ?? isoWholeSeconds.date(from: string)
    }

    /// True when the two dates fall on different calendar days (locally) —
    /// a day separator belongs between their rows.
    static func startsNewDay(_ date: Date, after previous: Date) -> Bool {
        !Calendar.current.isDate(date, inSameDayAs: previous)
    }

    /// The day-separator label: Today / Yesterday / "Monday, Jul 20".
    static func dayLabel(_ date: Date) -> String {
        if Calendar.current.isDateInToday(date) { return "Today" }
        if Calendar.current.isDateInYesterday(date) { return "Yesterday" }
        return dayName.string(from: date)
    }

    /// The row timestamp: "HH:mm" today, "Jul 20, HH:mm" on older days.
    static func timestamp(_ date: Date) -> String {
        Calendar.current.isDateInToday(date)
            ? timeOfDay.string(from: date)
            : dayAndTime.string(from: date)
    }
}
