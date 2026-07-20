import Foundation

/// Agent identity matching for thread headers — Swift mirror of the model
/// decided in NEXA-30 and documented in GOLDCOMB.md ("Agent identity model").
///
/// One name = one canonical identity, exact match otherwise; the only
/// equivalences are the read-time legacy aliases from the pre-rename era:
/// `nexais` ≡ `goldcomb` and `nexais-subagent:<label>` ≡
/// `goldcomb-subagent:<label>`. Aliases are applied at read time only — files
/// on disk are never rewritten.
enum AgentIdentity {
    /// Tool names that are interchangeable in thread headers.
    private static let legacyToolNames: Set<String> = ["goldcomb", "nexais"]

    /// Sub-agent id prefixes, canonical first.
    private static let subagentPrefixes = ["goldcomb-subagent:", "nexais-subagent:"]

    /// The set of header `agent` values that name the same identity as
    /// `name`: itself, plus its legacy alias when it is a tool name or a
    /// sub-agent id from the other naming era.
    static func equivalents(of name: String) -> Set<String> {
        var out: Set<String> = [name]
        if legacyToolNames.contains(name) {
            out.formUnion(legacyToolNames)
        } else {
            for prefix in subagentPrefixes where name.hasPrefix(prefix) {
                let label = String(name.dropFirst(prefix.count))
                out.formUnion(subagentPrefixes.map { $0 + label })
            }
        }
        return out
    }

    /// Does a thread whose header agent is `headerAgent` belong to the agent
    /// named `name`? Exact name match plus the legacy aliases above.
    static func matches(_ name: String, headerAgent: String) -> Bool {
        equivalents(of: name).contains(headerAgent)
    }

    /// Does a thread header name a sub-agent (`<tool>-subagent:<label>`,
    /// either era)? Sub-agent threads are attributed to the worker label, not
    /// the lead — they are excluded from a parent agent's own history list.
    static func isSubagent(_ headerAgent: String) -> Bool {
        subagentPrefixes.contains { headerAgent.hasPrefix($0) }
    }
}
