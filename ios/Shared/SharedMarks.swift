import Foundation

/// The last sighting, shared between the app and its widget through an App Group, so the widget can
/// show that a tap was taken. Optional by design: a signing that keeps no App Group leaves the widget
/// as plain buttons, and nothing else changes.
enum SharedMarks {
    /// The group this project asks for. A re-signing service may rename it to its own team's group,
    /// so the group actually granted is also read from the installed provisioning profile.
    static let requestedGroup = "group.nl.slot17.app"
    static let key = "lastMark"

    struct Last: Codable, Equatable {
        var kind: String
        var title: String
        var at: Date
    }

    /// The group this install can really use, or nil.
    static let group: String? = {
        let fm = FileManager.default
        if fm.containerURL(forSecurityApplicationGroupIdentifier: requestedGroup) != nil { return requestedGroup }
        for candidate in profileGroups() where fm.containerURL(forSecurityApplicationGroupIdentifier: candidate) != nil {
            return candidate
        }
        return nil
    }()

    /// Groups named in this bundle's embedded provisioning profile, if it has one.
    static func profileGroups(bundle: Bundle = .main) -> [String] {
        guard let url = bundle.url(forResource: "embedded", withExtension: "mobileprovision"),
              let data = try? Data(contentsOf: url) else { return [] }
        return groups(inProfile: data)
    }

    /// The profile is a signed container with a plain XML plist inside; only that plist is read.
    static func groups(inProfile data: Data) -> [String] {
        guard let text = String(data: data, encoding: .isoLatin1),
              let start = text.range(of: "<?xml"), let end = text.range(of: "</plist>", range: start.lowerBound..<text.endIndex),
              let xml = text[start.lowerBound..<end.upperBound].data(using: .isoLatin1),
              let plist = try? PropertyListSerialization.propertyList(from: xml, format: nil) as? [String: Any],
              let entitlements = plist["Entitlements"] as? [String: Any],
              let groups = entitlements["com.apple.security.application-groups"] as? [String] else { return [] }
        // Prefer one that is clearly this app's; a wildcard profile may list others.
        return groups.sorted { a, b in a.contains("slot17") && !b.contains("slot17") }
    }

    private static var defaults: UserDefaults? { group.flatMap { UserDefaults(suiteName: $0) } }

    static func save(_ last: Last) {
        guard let defaults, let data = try? JSONEncoder().encode(last) else { return }
        defaults.set(data, forKey: key)
    }

    static func load() -> Last? {
        guard let data = defaults?.data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(Last.self, from: data)
    }
}

/// What the widget shows about the last tap, and when that changes. Pure, so it is tested.
enum MarkWidgetModel {
    /// The tapped button carries a tick this long.
    static let tickFor: TimeInterval = 60
    /// The "last marked" line stays this long.
    static let lineFor: TimeInterval = 30 * 60

    struct Look: Equatable {
        var tickedKind: String?
        var line: SharedMarks.Last?
    }

    static func look(_ last: SharedMarks.Last?, at date: Date) -> Look {
        guard let last else { return Look(tickedKind: nil, line: nil) }
        let age = date.timeIntervalSince(last.at)
        guard age >= -5 else { return Look(tickedKind: nil, line: nil) }
        return Look(tickedKind: age < tickFor ? last.kind : nil, line: age < lineFor ? last : nil)
    }

    /// The moments the widget must redraw by itself: now, when the tick goes, when the line goes.
    static func changes(_ last: SharedMarks.Last?, from now: Date) -> [Date] {
        var dates = [now]
        if let last {
            for edge in [last.at.addingTimeInterval(tickFor), last.at.addingTimeInterval(lineFor)] where edge > now {
                dates.append(edge)
            }
        }
        return dates
    }
}
