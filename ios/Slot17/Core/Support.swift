import Foundation

/// One drive on the car computer (a session folder) with its corrected start: folder names can be wrong (1.5.0).
struct DriveRow: Decodable, Equatable, Identifiable {
    var session: String
    var start: String?
    var minutes: Double?
    var matches: Int?
    var faults: Int?
    var mb: Double?
    var recordings: Int?
    var id: String { session }
}

/// A support package on its way from the car computer: "building", or "uploading" with how far it got.
struct SupportProgress: Decodable, Equatable {
    var state: String
    var sent: Int64?
    var total: Int64?
}

/// A support package kept on the server, as `GET /api/support` lists it.
struct SupportPackage: Decodable, Equatable, Identifiable {
    var name: String
    var size: Int64
    var ageSeconds: Double
    var choice: String = ""
    var phone: [String] = []
    var teamCode: String?
    var teamExpires: String?
    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, size, choice, phone
        case ageSeconds = "age_s"
        case teamCode = "team_code"
        case teamExpires = "team_expires"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        size = try c.decode(Int64.self, forKey: .size)
        ageSeconds = try c.decode(Double.self, forKey: .ageSeconds)
        choice = try c.decodeIfPresent(String.self, forKey: .choice) ?? ""
        phone = try c.decodeIfPresent([String].self, forKey: .phone) ?? []
        teamCode = try c.decodeIfPresent(String.self, forKey: .teamCode)
        teamExpires = try c.decodeIfPresent(String.self, forKey: .teamExpires)
    }

    var withPlaces: Bool { phone.contains { $0.contains("places") } }
}

struct SupportListing: Decodable { var packages: [SupportPackage] }

/// The support package's words for the screens, the same on both phones (android core Support.kt).
enum SupportWords {
    static let choices: [(key: String, label: String)] = [("quick", "Quick check"), ("drive", "One drive"),
                                                          ("everything", "Everything from a drive")]
    /// The Slot17 team's inbox takes packages up to this size.
    static let teamMaxBytes: Int64 = 50_000_000

    static func choice(_ key: String) -> String { choices.first { $0.key == key }?.label ?? key }

    /// "20 Nov 19:40, 72 min, 37 matches, 2 faults" in this phone's time zone.
    static func drive(_ d: DriveRow, zone: TimeZone = .current) -> String {
        var parts: [String] = []
        if let s = d.start, let date = parseStart(s) {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_GB")
            f.timeZone = zone
            f.dateFormat = "d MMM HH:mm"
            parts.append(f.string(from: date))
        } else {
            parts.append("start unknown")
        }
        if let m = d.minutes { parts.append("\(Int(m)) min") }
        parts.append("\(d.matches ?? 0) matches")
        if let faults = d.faults, faults > 0 { parts.append("\(faults) fault\(faults == 1 ? "" : "s")") }
        return parts.joined(separator: ", ")
    }

    private static func parseStart(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }

    static func size(_ bytes: Int64) -> String {
        bytes >= 1_000_000 ? String(format: "%.1f MB", Double(bytes) / 1e6) : "\((bytes + 999) / 1000) KB"
    }

    /// What the car computer is doing with a package, or nil when nothing is on its way.
    static func progress(_ p: SupportProgress?) -> String? {
        switch p?.state {
        case "building": return "The car computer is making the package…"
        case "uploading":
            let total = p?.total ?? 0
            return total > 0 ? "Uploading \(size(p?.sent ?? 0)) of \(size(total))…" : "Uploading…"
        default: return nil
        }
    }

    enum WindowProblem: Error, Equatable { case invalid }

    /// A window in minutes after the drive's start, from two fields; nil for the whole drive.
    static func window(_ from: String, _ to: String) -> Result<[Int]?, WindowProblem> {
        let a = from.trimmingCharacters(in: .whitespaces), b = to.trimmingCharacters(in: .whitespaces)
        if a.isEmpty && b.isEmpty { return .success(nil) }
        guard let x = Int(a), let y = Int(b), x >= 0, y <= 1440, x < y else { return .failure(.invalid) }
        return .success([x, y])
    }

    static let windowHint = "Give two minutes after the start, the first smaller, up to 1440"
}
