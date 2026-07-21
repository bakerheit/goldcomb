import SwiftUI

/// A block of parsed Markdown. Agents emit Markdown constantly (headings,
/// lists, bold, quotes, fenced code); SwiftUI's `AttributedString(markdown:)`
/// only handles *inline* syntax, so blocks like `## Heading` and `- item` came
/// through raw. This models the block structure so it can render properly.
enum MarkdownBlock: Equatable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case bulletList([String])
    case orderedList([MarkdownOrderedItem])
    case quote(String)
    case code(String)
    case rule
}

struct MarkdownOrderedItem: Equatable {
    /// The source number, preserved so "11." shows 11 (not a re-count).
    let number: String
    let text: String
}

/// Line-oriented Markdown block parser. Pure and deterministic, so the block
/// segmentation is unit-tested independently of the SwiftUI rendering. Inline
/// syntax (bold/italic/code/links) is left in each block's text and handled at
/// render time by `AttributedString(markdown:)`.
enum Markdown {
    static func parse(_ source: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        let lines = source.components(separatedBy: "\n")
        var i = 0
        while i < lines.count {
            let line = lines[i].trimmingCharacters(in: .whitespaces)
            if line.isEmpty { i += 1; continue }

            if line.hasPrefix("```") {  // fenced code — take verbatim to the close
                var body: [String] = []
                i += 1
                while i < lines.count,
                      !lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    body.append(lines[i]); i += 1
                }
                if i < lines.count { i += 1 }  // consume the closing fence
                blocks.append(.code(body.joined(separator: "\n")))
                continue
            }
            if isRule(line) { blocks.append(.rule); i += 1; continue }
            if let (level, text) = heading(line) {
                blocks.append(.heading(level: level, text: text)); i += 1; continue
            }
            if let q = quoteText(line) {
                var parts = [q]; i += 1
                while i < lines.count,
                      let q2 = quoteText(lines[i].trimmingCharacters(in: .whitespaces)) {
                    parts.append(q2); i += 1
                }
                blocks.append(.quote(parts.joined(separator: "\n")))
                continue
            }
            if let item = bulletItem(line) {
                var items = [item]; i += 1
                while i < lines.count,
                      let it = bulletItem(lines[i].trimmingCharacters(in: .whitespaces)) {
                    items.append(it); i += 1
                }
                blocks.append(.bulletList(items))
                continue
            }
            if let item = orderedItem(line) {
                var items = [item]; i += 1
                while i < lines.count,
                      let it = orderedItem(lines[i].trimmingCharacters(in: .whitespaces)) {
                    items.append(it); i += 1
                }
                blocks.append(.orderedList(items))
                continue
            }
            // A paragraph runs until a blank line or the start of another block.
            var para = [line]; i += 1
            while i < lines.count {
                let l = lines[i].trimmingCharacters(in: .whitespaces)
                if l.isEmpty || l.hasPrefix("```") || isRule(l) || heading(l) != nil
                    || quoteText(l) != nil || bulletItem(l) != nil
                    || orderedItem(l) != nil { break }
                para.append(l); i += 1
            }
            blocks.append(.paragraph(para.joined(separator: "\n")))
        }
        return blocks
    }

    // MARK: block matchers

    private static func heading(_ line: String) -> (Int, String)? {
        guard line.hasPrefix("#") else { return nil }
        var level = 0
        for ch in line { if ch == "#" { level += 1 } else { break } }
        guard (1...6).contains(level) else { return nil }
        let rest = line.dropFirst(level)
        guard rest.first == " " else { return nil }  // "#tag" is not a heading
        var text = rest.trimmingCharacters(in: .whitespaces)
        while text.hasSuffix("#") { text.removeLast() }  // closing ###
        return (level, text.trimmingCharacters(in: .whitespaces))
    }

    private static func bulletItem(_ line: String) -> String? {
        guard let f = line.first, "-*+".contains(f) else { return nil }
        let rest = line.dropFirst()
        guard rest.first == " " else { return nil }  // "-x"/"*bold*" aren't items
        return rest.trimmingCharacters(in: .whitespaces)
    }

    private static func orderedItem(_ line: String) -> MarkdownOrderedItem? {
        var idx = line.startIndex
        var digits = ""
        while idx < line.endIndex, line[idx].isNumber {
            digits.append(line[idx]); idx = line.index(after: idx)
        }
        guard !digits.isEmpty, idx < line.endIndex,
              line[idx] == "." || line[idx] == ")" else { return nil }
        idx = line.index(after: idx)
        guard idx < line.endIndex, line[idx] == " " else { return nil }
        return MarkdownOrderedItem(
            number: digits,
            text: String(line[idx...]).trimmingCharacters(in: .whitespaces))
    }

    private static func quoteText(_ line: String) -> String? {
        guard line.hasPrefix(">") else { return nil }
        var rest = line.dropFirst()
        if rest.first == " " { rest = rest.dropFirst() }
        return String(rest)
    }

    private static func isRule(_ line: String) -> Bool {
        guard line.count >= 3 else { return false }
        let set = Set(line)
        guard set.count == 1, let c = set.first, "-*_".contains(c) else { return false }
        return true
    }
}

/// Renders a Markdown string as stacked SwiftUI blocks. `decorate` is an
/// optional post-process over each inline run's AttributedString — the chat
/// room passes one that adds ticket-id links and the @user highlight, so those
/// survive the switch to Markdown rendering.
struct MarkdownMessage: View {
    let text: String
    var decorate: ((AttributedString) -> AttributedString)? = nil

    private var blocks: [MarkdownBlock] { Markdown.parse(text) }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                render(block)
            }
        }
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func render(_ block: MarkdownBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            inline(text).font(headingFont(level))
        case .paragraph(let text):
            inline(text)
        case .bulletList(let items):
            VStack(alignment: .leading, spacing: 3) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•").foregroundStyle(.secondary)
                        inline(item)
                    }
                }
            }
        case .orderedList(let items):
            VStack(alignment: .leading, spacing: 3) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("\(item.number).").foregroundStyle(.secondary).monospacedDigit()
                        inline(item.text)
                    }
                }
            }
        case .quote(let text):
            HStack(spacing: 8) {
                RoundedRectangle(cornerRadius: 1)
                    .fill(.secondary.opacity(0.4)).frame(width: 3)
                inline(text).foregroundStyle(.secondary)
            }
        case .code(let code):
            Text(code)
                .font(.system(.caption, design: .monospaced))
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(.quaternary.opacity(0.5),
                            in: RoundedRectangle(cornerRadius: 6))
        case .rule:
            Divider()
        }
    }

    private func inline(_ s: String) -> Text {
        var attr = (try? AttributedString(markdown: s, options: .init(
            interpretedSyntax: .inlineOnlyPreservingWhitespace,
            failurePolicy: .returnPartiallyParsedIfPossible))) ?? AttributedString(s)
        if let decorate { attr = decorate(attr) }
        return Text(attr)
    }

    private func headingFont(_ level: Int) -> Font {
        switch level {
        case 1: return .title3.weight(.bold)
        case 2: return .headline
        default: return .subheadline.weight(.semibold)
        }
    }
}
