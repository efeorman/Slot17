import Foundation

/// The private link the operator pastes in, split into server address and key.
struct ServerLink: Equatable {
    let base: URL
    let key: String

    /// Accepts `http://host:port/?key=...`, with or without a trailing slash or spaces.
    static func parse(_ text: String) -> ServerLink? {
        let cleaned = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let parts = URLComponents(string: cleaned),
              let scheme = parts.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = parts.host, !host.isEmpty,
              let key = parts.queryItems?.first(where: { $0.name == "key" })?.value, key.count >= 8,
              key.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") })
        else { return nil }
        var base = URLComponents()
        base.scheme = scheme
        base.host = host
        base.port = parts.port
        guard let url = base.url else { return nil }
        return ServerLink(base: url, key: key)
    }
}

/// A sighting the operator reported. The phone keeps it until the Pi has confirmed it.
struct Sighting: Codable, Identifiable, Equatable {
    enum Label: String, Codable, CaseIterable, Identifiable {
        // "noise" is the detector's own word for "nothing seen"; p0/mark.py accepts it.
        case police, ambulance, fire, unknown, nothing = "noise"
        var id: String { rawValue }
        var title: String {
            switch self {
            case .police: return "Police"
            case .ambulance: return "Ambulance"
            case .fire: return "Fire"
            case .unknown: return "Other"
            case .nothing: return "Saw nothing"
            }
        }
        /// An emergency vehicle was actually seen.
        var isVehicle: Bool { self == .police || self == .ambulance || self == .fire }
        /// The vehicle buttons, without "Saw nothing", which has its own wider button.
        static var vehicles: [Label] { [.police, .ambulance, .fire, .unknown] }
    }
    enum Phase: String, Codable {
        case sending, waitingForPi, saved, failed
    }
    var id: String
    var label: Label
    var note: String
    var seenAt: Date
    var phase: Phase
    var detail: String
    var isRecordingRequest: Bool
    var place: Place?

    static func newID() -> String {
        UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    }

    /// The same button again within this long is the same sighting: on 11 November 2026 one police
    /// car was saved twice, 1.5 s apart, from a double tap on the CarPlay widget.
    static let repeatWindow: TimeInterval = 3

    static func isRepeat(_ label: Label, at seenAt: Date, among sightings: [Sighting]) -> Bool {
        sightings.contains { $0.label == label && !$0.isRecordingRequest && abs($0.seenAt.timeIntervalSince(seenAt)) < repeatWindow }
    }

    /// The server may forget a sighting if it restarts, and the Pi ignores a repeat by id.
    var needsSending: Bool { phase == .sending || phase == .waitingForPi }

    var statusText: String {
        switch phase {
        case .sending: return "Sending…"
        case .waitingForPi: return detail.ifEmpty("Waiting for the Pi")
        case .saved, .failed: return detail
        }
    }

    mutating func apply(state: String, detail newDetail: String) {
        // Saved stays saved: from 1.6.0 a sighting can go by both routes, and the server's copy may expire later.
        if phase == .saved && state != "done" { return }
        switch state {
        case "pending", "sent":
            phase = .waitingForPi
            detail = state == "sent" ? "The Pi has it" : "Waiting for the Pi"
        case "done":
            phase = .saved
            detail = newDetail.ifEmpty("Saved on the Pi")
        case "failed", "expired":
            phase = .failed
            detail = newDetail.ifEmpty("Not saved")
        default:
            break
        }
    }
}

/// Where the phone was. Kept on the phone and sent with sightings, for later analysis only.
struct Place: Codable, Equatable {
    var lat: Double
    var lon: Double
    var speedKmh: Double?
    var accuracyM: Double?

    var noteText: String {
        var parts = [String(format: "lat=%.6f", lat), String(format: "lon=%.6f", lon)]
        if let speedKmh { parts.append(String(format: "kmh=%.0f", speedKmh)) }
        if let accuracyM { parts.append(String(format: "acc_m=%.0f", accuracyM)) }
        return parts.joined(separator: ";")
    }
}

struct AlertRecord: Codable, Identifiable, Equatable {
    var id: String
    var at: Date
    var channel: Int
    var strength: Double?
    var age: Double
    var sound: SoundKind?
    var reason: String
    var place: Place?
    /// Which recognition rule matched (car computer 1.2.9 and later); nil for older records.
    var rule: String? = nil
}

/// What the operator's marks say about the sounds: the honest answer to "are the beeps useful?"
struct MarkSummary: Equatable {
    struct Row: Equatable {
        var played = 0
        var vehicleSeen = 0
        var nothingSeen = 0
        var unmarked: Int { played - vehicleSeen - nothingSeen }
    }
    var urgent = Row()
    var chime = Row()
    /// Vehicles marked with no sound at all in the window before: possible misses.
    var vehiclesWithoutSound = 0
    var vehiclesMarked = 0

    /// Each sound counts towards the first mark made within `window` seconds after it.
    static func make(alerts: [AlertRecord], sightings: [Sighting], window: TimeInterval) -> MarkSummary {
        var out = MarkSummary()
        let marks = sightings.filter { !$0.isRecordingRequest }.sorted { $0.seenAt < $1.seenAt }
        for alert in alerts {
            guard let kind = alert.sound, kind == .urgent || kind == .chime else { continue }
            let next = marks.first { $0.seenAt >= alert.at && $0.seenAt.timeIntervalSince(alert.at) <= window }
            var row = kind == .urgent ? out.urgent : out.chime
            row.played += 1
            if let next {
                if next.label.isVehicle { row.vehicleSeen += 1 }
                if next.label == .nothing { row.nothingSeen += 1 }
            }
            if kind == .urgent { out.urgent = row } else { out.chime = row }
        }
        for mark in marks where mark.label.isVehicle {
            out.vehiclesMarked += 1
            let heard = alerts.contains {
                $0.sound != nil && $0.at <= mark.seenAt && mark.seenAt.timeIntervalSince($0.at) <= window
            }
            if !heard { out.vehiclesWithoutSound += 1 }
        }
        return out
    }
}
