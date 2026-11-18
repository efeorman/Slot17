import Foundation

/// A widget tap that arrived while the app was starting in the background, without its model. Saved
/// before the tap returns, and turned into a normal sighting when the app next starts properly.
struct PendingMark: Codable, Equatable {
    var label: Sighting.Label
    var seenAt: Date
    var place: Place?
    /// Becomes the sighting's id, so it cannot be imported twice.
    var id: String? = nil
}

struct MarkInbox {
    let url: URL

    static var standard: MarkInbox {
        MarkInbox(url: FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("pending-marks.json"))
    }

    var all: [PendingMark] {
        guard let data = try? Data(contentsOf: url) else { return [] }
        return (try? JSONDecoder().decode([PendingMark].self, from: data)) ?? []
    }

    /// Written at once, and readable while the phone is locked after its first unlock.
    func add(_ mark: PendingMark) {
        var marks = all
        marks.append(mark)
        if marks.count > 200 { marks.removeFirst(marks.count - 200) }
        guard let data = try? JSONEncoder().encode(marks) else { return }
        try? data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    func clear() { try? FileManager.default.removeItem(at: url) }
}
