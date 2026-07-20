import Foundation

/// Human names for agents — mirrors goldcomb/names.py so both creation
/// surfaces mint from the same pool. Every agent gets a First Last: named
/// colleagues read better on the board and in the team tree than slugs.
enum Names {
    static let first = [
        "Ada", "Amos", "Anouk", "Aria", "Basil", "Beatrix", "Callum", "Cleo",
        "Dara", "Della", "Edwin", "Effie", "Felix", "Freya", "Gideon", "Greta",
        "Hollis", "Ines", "Ivo", "Juniper", "Kai", "Lena", "Linus", "Maeve",
        "Marlowe", "Maya", "Nadia", "Nico", "Opal", "Otis", "Petra", "Quill",
        "Rafael", "Romy", "Silas", "Sonia", "Tamsin", "Theo", "Vera", "Wren",
    ]

    static let last = [
        "Ambrose", "Ashwood", "Beckett", "Birch", "Calloway", "Cardew",
        "Danforth", "Eastley", "Fenn", "Foxglove", "Gable", "Greenlaw", "Hale",
        "Harlow", "Ibsen", "Juno", "Kestrel", "Larkspur", "Mercer", "Moss",
        "Northgate", "Oakes", "Pemberly", "Quimby", "Rook", "Sable", "Sorrel",
        "Thistle", "Trellis", "Umber", "Vale", "Wilder", "Winslow", "Yarrow",
        "Zephyr",
    ]

    /// A fresh "First Last", avoiding the names already in use when possible.
    static func random(avoiding taken: Set<String> = []) -> String {
        for _ in 0..<24 {
            let name = "\(first.randomElement()!) \(last.randomElement()!)"
            if !taken.contains(name) { return name }
        }
        return "\(first.randomElement()!) \(last.randomElement()!)"
    }
}
