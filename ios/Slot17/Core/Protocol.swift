import Foundation

/// What the display server sends, as it sends it. Unknown fields are ignored and optional
/// ones may be missing, so an older Pi publisher keeps working with this app.
struct ServerMessage: Decodable {
    let boot: String
    let seq: Int
    let serverMono: Double
    let serverAge: Double?
    let state: PiState?
    let command: CommandStatus?
    let ops: [OpStatus]?
    /// The newest published release (server 1.2.9 and later, once the repository is public).
    var release: ReleaseInfo? = nil

    enum CodingKeys: String, CodingKey {
        case boot, seq, state, command, ops, release
        case serverMono = "server_mono_s"
        case serverAge = "server_age_s"
    }
}

struct PiState: Decodable {
    var source: String?
    var session: String?
    var snapshotAge: Double
    var detector: DetectorState
    var channels: [ChannelRow]
    var matches: [MatchRow]
    var features: [String]?
    var pi: PiHealth?
    var update: UpdateState? = nil
    var link: PiLink? = nil
    /// Whether the car computer sends Pushover messages (release 1.2.9 and later).
    var notify: NotifySwitch? = nil
    /// The detector's software version, for example "1.2.9" (1.2.9 and later).
    var software: String? = nil

    enum CodingKeys: String, CodingKey {
        case source, session, detector, channels, matches, features, pi, update, link, notify, software
        case snapshotAge = "snapshot_age_s"
    }
}

struct DetectorState: Decodable {
    var state: String
    var heartbeatUnchanged: Double?
    var reasons: [String]?
    var windows: Int?
    var hits: Int?
    var unchecked: Int?
    var partial: Int?
    var quietWindows: Int?
    var inputPeak: Double?
    /// How many quiet windows in a row the Pi itself counts as "receiver hears nothing". It depends
    /// on the detector's window length, so the Pi's own figure is used when it sends one.
    var quietAlarmWindows: Int? = nil
    /// The band's quiet level in the detector's own dB (not dBm). Lower is better.
    var floorDB: Double? = nil
    /// Windows so far in which 60 or more channels fired at once: interference, not radios.
    var wholeBandWindows: Int? = nil

    enum CodingKeys: String, CodingKey {
        case state, reasons, windows, hits, unchecked, partial
        case floorDB = "floor_db"
        case wholeBandWindows = "whole_band_windows"
        case quietAlarmWindows = "quiet_alarm_windows"
        case heartbeatUnchanged = "heartbeat_unchanged_s"
        case quietWindows = "quiet_windows"
        case inputPeak = "input_peak_counts"
    }
}

struct ChannelRow: Decodable, Identifiable {
    var ch: Int
    var freqMHz: Double
    var strength: Double?
    var age: Double
    var hits60: Int
    var approxTime: Bool?
    /// "active" or "intermittent": burst density measured by the detector, not decoded content.
    var activity: String? = nil
    /// The Pi's id for the latest event on this channel: tells one burst from the next.
    var burstID: String? = nil
    /// Getting louder: the newest match is 6 dB or more above the minute's weakest, and the strongest.
    /// Louder is not nearer: a radio sets its power by its own mast.
    var rising: Bool? = nil
    /// A call pattern on the channel in the last 10 s: normal bursts on every frame. Traffic, not identity.
    var call: Bool? = nil
    var id: Int { ch }

    enum CodingKeys: String, CodingKey {
        case ch
        case burstID = "id"
        case freqMHz = "freq_mhz"
        case strength = "strength_db"
        case age = "age_s"
        case hits60 = "hits_60s"
        case approxTime = "approx_time"
        case activity, rising, call
    }
}

struct MatchRow: Decodable {
    var id: String
    var ch: Int
    var strength: Double?
    var age: Double
    /// This match sits in a call pattern (normal bursts on every frame); see ChannelRow.call.
    var call: Bool? = nil
    var rising: Bool? = nil
    /// How the training word was recognised: "standard", "weak" or "coherent" (1.2.9 and later).
    /// A record of how, not of how close, and never of who.
    var rule: String? = nil

    enum CodingKeys: String, CodingKey {
        case id, ch, call, rising, rule
        case strength = "strength_db"
        case age = "age_s"
    }
}

/// What the car computer's reports travel over, from its publisher (release 9 and later). Every field optional.
struct PiLink: Decodable, Equatable {
    struct Stick: Decodable, Equatable {
        var connected: Bool?
        var network: String?
        var provider: String?
        var roaming: Bool?
        var bars: Double?
        var rsrp: Double?
        var rsrq: Double?
        var snr: Double?
        var cell: String?
        var pingMS: Double?
        var pingWorstMS: Double?
        var pingLost: Double?
        var downS: Double?
        enum CodingKeys: String, CodingKey {
            case connected, network, provider, roaming, bars, rsrp, rsrq, snr, cell
            case pingMS = "ping_ms", pingWorstMS = "ping_worst_ms", pingLost = "ping_lost", downS = "down_s"
        }
    }
    var via: String?
    var networks: [String]?
    var device: String?
    var pushes: Double?
    var failed: Double?
    var failovers: Double?
    var medianMS: Double?
    var slowestMS: Double?
    var wifiDBm: Double?
    var clockSynced: Bool?
    var stick: Stick?
    enum CodingKeys: String, CodingKey {
        case via, networks, device, pushes, failed, failovers, stick
        case medianMS = "median_ms", slowestMS = "slowest_ms", wifiDBm = "wifi_dbm", clockSynced = "clock_synced"
    }
}

extension PiLink {
    /// The network in use, in words.
    var viaText: String {
        let name: String
        switch via {
        case "4G stick"?: name = "the 4G stick"
        case "Wi-Fi"?: name = "Wi-Fi (hotspot)"
        case "cable"?: name = "a network cable"
        case "none"?: name = "no network"
        default: name = via ?? "unknown"
        }
        return device.map { "\(name) (\($0))" } ?? name
    }
}

/// Plain words for a 4G signal. The usual LTE ranges, not a measurement of this car.
enum SignalWords {
    static func rsrp(_ dBm: Double) -> String {
        switch dBm {
        case (-90)...: return "good"
        case (-105)..<(-90): return "fair"
        case (-115)..<(-105): return "weak"
        default: return "very weak"
        }
    }
    static func wifi(_ dBm: Double) -> String {
        switch dBm {
        case (-60)...: return "good"
        case (-70)..<(-60): return "fair"
        default: return "weak"
        }
    }
}

/// The car computer's Pushover switch, as it reports it. `configured` is false where Pushover was never set up.
struct NotifySwitch: Decodable, Equatable {
    var pushover: Bool
    var configured: Bool?
}

struct PiHealth: Decodable {
    var uptime: Double?
    var temperature: Double?
    var load: Double?
    var diskFreeGB: Double?
    var memFreeMB: Double?
    var sessionSeconds: Double?
    var droppedSamples: Double?

    enum CodingKeys: String, CodingKey {
        case uptime = "uptime_s"
        case temperature = "temp_c"
        case load = "load_1m"
        case diskFreeGB = "disk_free_gb"
        case memFreeMB = "mem_free_mb"
        case sessionSeconds = "session_s"
        case droppedSamples = "dropped_samples"
    }
}

/// The car computer's software update, as its publisher reports it: "current", "installed" (at this
/// start), "staged" (waits for the next start), "rejected", "error" or "off".
struct UpdateState: Decodable, Equatable {
    var state: String
    var commit: String?
    var seq: Int?
    var detail: String?
}

struct CommandStatus: Decodable {
    var action: String?
    var state: String
    var age: Double?

    enum CodingKeys: String, CodingKey {
        case action, state
        case age = "age_s"
    }
}

struct OpStatus: Decodable {
    var id: String
    var kind: String
    var state: String
    var detail: String
}

struct ServerLogEntry: Decodable, Identifiable {
    var name: String
    var size: Int
    var ageSeconds: Double
    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, size
        case ageSeconds = "age_s"
    }
}

/// Turns the server's event stream into one JSON text per message.
struct SSEParser {
    private var pending = Data()

    mutating func feed(_ data: Data) -> [String] {
        pending.append(data)
        if pending.count > 2_000_000 { pending.removeAll() }
        var messages: [String] = []
        while let end = pending.range(of: Data([10, 10])) {
            let block = pending.subdata(in: pending.startIndex..<end.lowerBound)
            pending.removeSubrange(pending.startIndex..<end.upperBound)
            guard let text = String(data: block, encoding: .utf8) else { continue }
            let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
                .filter { $0.hasPrefix("data:") }
                .map { $0.dropFirst(5).drop(while: { $0 == " " }) }
            if !lines.isEmpty { messages.append(lines.joined(separator: "\n")) }
        }
        return messages
    }
}
