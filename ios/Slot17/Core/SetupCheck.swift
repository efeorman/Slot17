import Foundation

/// The car computer's own `setup.sh doctor` verdicts, as [part, level, text] (car computer 1.3.1 and later).
struct DoctorReport: Decodable, Equatable {
    var items: [[String]]
    var age: Double

    enum CodingKeys: String, CodingKey {
        case items
        case age = "age_s"
    }

    init(items: [[String]], age: Double = 0) {
        self.items = items
        self.age = age
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        items = (try? c.decode([[String]].self, forKey: .items)) ?? []
        age = (try? c.decode(Double.self, forKey: .age)) ?? 0
    }
}

struct CheckRow: Equatable, Identifiable {
    var name: String
    var level: Level
    var text: String
    var fix: String?
    var id: String { name + text }
}

/// "Check my setup": every link from this phone to the receiver in plain words, with what to do when one
/// isn't right. The same rules as android/core SetupCheck.kt, with the iPhone's own items (sound armed).
enum SetupCheck {
    static func phone(hasLink: Bool, chain: [ChainItem], notifications: String, armed: Bool, sound: Bool) -> [CheckRow] {
        guard hasLink else {
            return [CheckRow(name: "Private link", level: .bad, text: "Not added",
                             fix: "Settings, Add your private link. You get it from /server create in the Slot17 Discord, or from your own server's setup.")]
        }
        var rows = [CheckRow(name: "Private link", level: .ok, text: "Added")]
        for item in chain {
            rows.append(CheckRow(name: item.name, level: item.level, text: item.detail,
                                 fix: item.level == .bad || item.level == .warn ? fix(for: item.id) : nil))
        }
        rows.append(sound ? CheckRow(name: "Sound", level: .ok, text: "On") : CheckRow(name: "Sound", level: .warn, text: "Off", fix: "Settings, Sound."))
        rows.append(armed ? CheckRow(name: "Sound with the phone locked", level: .ok, text: "Armed")
                    : CheckRow(name: "Sound with the phone locked", level: .warn, text: "Not armed yet",
                               fix: "Open the app once before you drive, then lock the phone. iOS doesn't let an app start sound from the background."))
        if notifications != "Allowed" && notifications != "Not asked yet" {
            rows.append(CheckRow(name: "Notifications", level: .warn, text: notifications,
                                 fix: "Only needed for fault messages from the app: iPhone Settings, Slot17, Notifications."))
        }
        return rows
    }

    static func fix(for id: String) -> String {
        switch id {
        case "server": return "Check that this phone is online. With your own server: is it running, and is port 5732 open in its firewall?"
        case "pi": return "Is the car computer powered, and on your phone's hotspot? Its server address and key must match your server."
        case "detector": return "If it was stopped, it starts again at the next power-up. If it has a fault, the car computer's own check below says which."
        case "receiver": return "Check the Airspy's USB cable and the antenna cable. Unplugging the Airspy for a moment can help when it hangs."
        default: return "See the car computer's own check below."
        }
    }

    static func car(_ report: DoctorReport?) -> [CheckRow] {
        guard let report else {
            return [CheckRow(name: "Car computer", level: .idle, text: "Not checked yet",
                             fix: "It checks itself about a minute and a half after it starts (car computer software 1.3.1 and later).")]
        }
        return report.items.filter { $0.count == 3 }.map { row in
            CheckRow(name: row[0], level: row[1] == "ok" ? .ok : row[1] == "PROBLEM" ? .bad : .warn, text: row[2])
        }
    }

    /// The first thing to fix: problems before warnings, the phone's side before the car's.
    static func first(_ rows: [CheckRow]) -> CheckRow? {
        rows.first { $0.level == .bad } ?? rows.first { $0.level == .warn }
    }
}
