import Foundation

enum Level: Equatable { case ok, warn, bad, idle }

struct HeadlineStatus: Equatable {
    var level: Level
    var title: String
    var detail: String
}

/// One link in the chain from this phone to the receiver.
struct ChainItem: Identifiable, Equatable {
    var id: String
    var name: String
    var level: Level
    var detail: String
}

struct MeterRow: Identifiable, Equatable {
    var ch: Int
    var freqMHz: Double
    var strength: Double?
    var age: Double
    var hits60: Int
    var approxTime: Bool
    /// The detector saw many bursts close together on this channel. Energy density only:
    /// not decoded, so not "speech" or "data", and several radios can add up to it.
    var frequent: Bool = false
    /// Getting louder over the last minute; louder is not nearer.
    var rising: Bool = false
    /// A call pattern (normal bursts on every frame) on this channel in the last 10 s.
    var call: Bool = false
    var id: Int { ch }
    /// An aircraft's channel (1.8.0): a radio in the air, not a vehicle on the road.
    var air: Bool { Aircraft.isAir(ch) }
}

/// What a meter's colour means: what the app would do, never how near or what kind of vehicle.
/// weak: heard, below the yellow level. yellow: heard, silent. red: chime level. grey: no data.
enum MeterColour: Equatable { case weak, yellow, red, grey }

enum Meters {
    static let segments = 10
    /// The strength at which the bar starts (the detector's 6 dB candidate threshold).
    static let floorDB = 6.0

    /// The bar is full only at the urgent level, so 36 dB and 59 dB no longer look the same.
    /// Every detection lights at least one segment; the top segment means "urgent level reached".
    static func level(_ strength: Double?, urgent: Double = 50) -> Int {
        guard let s = strength else { return 0 }
        if s >= urgent { return segments }
        let span = max(1, urgent - floorDB)
        let lit = Int(((s - floorDB) / span * Double(segments)).rounded(.up))
        return min(segments - 1, max(1, lit))
    }

    /// Colour by alert eligibility. Quiet mode's higher chime level counts.
    static func colour(_ strength: Double?, _ settings: AlertSettings) -> MeterColour {
        guard let s = strength else { return .grey }
        if s >= settings.effectiveRed { return .red }
        return s >= settings.yellow ? .yellow : .weak
    }

    /// At or above the urgent level: the meter gets an outline and a STRONG badge.
    static func isStrong(_ strength: Double?, _ settings: AlertSettings) -> Bool {
        guard let s = strength else { return false }
        return s >= settings.urgentLevel
    }

    /// A fresh strong burst may pulse the outline once; not again on the same channel for this long,
    /// so urgent bursts every second never become continuous flashing.
    static let pulseEvery: Double = 10
    static let pulseFreshAge: Double = 2

    /// Recent channels first (under 15 s), then strongest. Older than a minute is dropped.
    static func pick(_ rows: [MeterRow]) -> [MeterRow] {
        rows.filter { $0.age <= 60 }.sorted { a, b in
            let ra = a.age < 15, rb = b.age < 15
            if ra != rb { return ra }
            let sa = a.strength ?? -1e9, sb = b.strength ?? -1e9
            if sa != sb { return sa > sb }
            return a.age < b.age
        }
    }

    static func opacity(_ age: Double) -> Double {
        max(0.35, 1 - max(0, age - 10) / 50 * 0.65)
    }
}

/// How often whole-band interference came in about the last minute, from the Pi's running counts.
struct InterferenceMeter {
    private var samples: [(at: Double, windows: Int, wide: Int)] = []

    mutating func add(at now: Double, windows: Int, wide: Int) {
        if let last = samples.last, windows < last.windows { samples.removeAll() }   // a new session began
        samples.append((at: now, windows: windows, wide: wide))
        while samples.count > 2, now - samples[1].at >= 60 { samples.removeFirst() }
    }

    /// Percent of the windows in about the last minute, or nil until enough of them have passed.
    var percent: Double? {
        guard let first = samples.first, let last = samples.last, last.windows - first.windows >= 20 else { return nil }
        return 100 * Double(max(0, last.wide - first.wide)) / Double(last.windows - first.windows)
    }
}

enum Format {
    static func ago(_ s: Double) -> String {
        if !s.isFinite { return "never" }
        if s < 1 { return "now" }
        if s < 90 { return "\(Int(s.rounded())) s ago" }
        if s < 5400 { return "\(Int((s / 60).rounded())) min ago" }
        return "\(Int((s / 3600).rounded())) h ago"
    }

    /// "85.2 dB, 3.5 above the receiver alone": every dB above is a dB of reach lost to noise.
    static func floor(_ floor: Double, quiet: Double?) -> String {
        guard let quiet else { return String(format: "%.1f dB", floor) }
        return String(format: "%.1f dB, %.1f above the receiver alone", floor, max(0, floor - quiet))
    }

    /// The car computer's software, in a few words for the Pi screen.
    static func software(_ update: UpdateState) -> String {
        let commit = String((update.commit ?? "").prefix(7))
        let version = commit.isEmpty ? "as installed by hand" : commit
        let detail = update.detail ?? ""
        switch update.state {
        case "installed": return "\(version), updated at this start"
        case "staged": return "\(version); an update installs at the next start"
        case "rejected": return "\(version); an update failed its checks\(detail.isEmpty ? "" : ": \(detail)")"
        case "error": return "\(version); update problem\(detail.isEmpty ? "" : ": \(detail)")"
        default: return "\(version), up to date when last checked"
        }
    }

    static func duration(_ s: Double) -> String {
        let total = Int(s)
        let h = total / 3600, m = total % 3600 / 60
        if h > 0 { return "\(h) h \(m) min" }
        if m > 0 { return "\(m) min" }
        return "\(total) s"
    }
}

/// What Bluetooth says of the car computer (1.6.0), for when the server has nothing fresh from it: its detector
/// as the car computer sent it a moment ago.
struct BluetoothView {
    var detector: DetectorState
}

/// Plain-language state, the same decisions as the web page's status line.
enum StatusBuilder {
    static let quietAlarmWindows = 40

    /// `bluetooth` only when the server has nothing fresh from the car: then Bluetooth's word counts. Before 1.6.0
    /// the lights and the line read only the server, so on 23 November they said Pi silent while Bluetooth was live.
    /// `bluetoothSetUp`: this phone has the car computer's Bluetooth key, so without a server it waits for that.
    static func headline(_ t: LinkTracker, now: Double, lastDelay: Double?, hasCredentials: Bool,
                         bluetooth: BluetoothView? = nil, bluetoothSetUp: Bool = false) -> HeadlineStatus {
        if let b = bluetooth { return detectorLine(b.detector, lastDelay: lastDelay, demo: false, overBluetooth: true) }
        if !hasCredentials {
            if bluetoothSetUp { return .init(level: .idle, title: "Waiting for the car computer", detail: "Over Bluetooth, once it is on and in range") }
            return .init(level: .idle, title: "Not set up", detail: "Add your private link, or set up Bluetooth, in Settings")
        }
        if t.authFailed { return .init(level: .bad, title: "Link not accepted", detail: "Check your private link in Settings") }
        guard let f = t.frame else { return .init(level: .idle, title: "Connecting…", detail: "Looking for the server") }
        let rx = t.sinceRx(now)
        if rx > LinkTracker.linkLost {
            return .init(level: .idle, title: "Connection lost", detail: "No update for \(Int(rx)) s, reconnecting")
        }
        guard f.message.serverAge != nil, let st = f.message.state else {
            return .init(level: .idle, title: "Waiting for the car", detail: "The server has heard nothing from the Pi yet")
        }
        if !t.clockReady(now) { return .init(level: .idle, title: "Checking timing", detail: "Waiting for a fresh timing sample") }
        let carAge = t.carAge(now)
        if carAge > LinkTracker.carStale {
            return .init(level: .idle, title: "No data from the car", detail: "Last update \(Format.ago(carAge)). Hotspot or Pi offline")
        }
        return detectorLine(st.detector, lastDelay: lastDelay, demo: t.demo, overBluetooth: false)
    }

    /// The line for the detector's own state, whichever route told it.
    static func detectorLine(_ d: DetectorState, lastDelay: Double?, demo: Bool, overBluetooth: Bool) -> HeadlineStatus {
        let via = overBluetooth ? " (over Bluetooth: no link to the server)" : ""
        switch d.state {
        case "running":
            if overBluetooth {
                return .init(level: .ok, title: "Live over Bluetooth", detail: "No link to the server: everything comes straight from the car computer")
            }
            let detail = lastDelay.map { String(format: "Delay up to %.1f s after the burst", ($0 * 10).rounded(.up) / 10) }
                ?? "\(d.windows ?? 0) windows analysed"
            return .init(level: .ok, title: demo ? "Demo running" : "Detector running", detail: demo ? "Fake data, nothing is being detected" : detail)
        case "starting":
            return .init(level: .idle, title: "Detector starting", detail: "Waiting for the first analysis" + via)
        case "degraded":
            return .init(level: .warn, title: "Detector degraded", detail: (d.reasons ?? []).joined(separator: ", ").ifEmpty("See status on the Pi") + via)
        case "stale":
            return .init(level: .bad, title: "Detector not responding", detail: "Heartbeat unchanged for \(Int(d.heartbeatUnchanged ?? 0)) s" + via)
        case "stopped":
            // Stopped by itself (1.6.0 says why, from the session's summary): a fault, not a quiet road.
            if let reason = d.reasons?.first, !reason.isEmpty {
                return .init(level: .bad, title: "Detector stopped by itself", detail: "\(reason). It starts again when the Pi next powers up" + via)
            }
            return .init(level: .idle, title: "Detector stopped", detail: "It starts again when the Pi next powers up" + via)
        default:
            return .init(level: .idle, title: "Unknown state", detail: d.state + via)
        }
    }

    static func receiverQuiet(_ t: LinkTracker, settings: AlertSettings, now: Double, bluetooth: BluetoothView? = nil) -> Bool {
        guard !t.demo, settings.antennaWarning else { return false }
        if let b = bluetooth {
            guard b.detector.state == "running" || b.detector.state == "degraded" else { return false }
            return (b.detector.quietWindows ?? 0) >= quietLimit(b.detector, settings)
        }
        guard t.detectorActive else { return false }
        // Old data says nothing about the receiver now; losing the link has its own warning.
        guard t.carAge(now) <= LinkTracker.carStale else { return false }
        guard let detector = t.frame?.message.state?.detector else { return false }
        return (detector.quietWindows ?? 0) >= quietLimit(detector, settings)
    }

    /// The Pi's own limit when it sends one; the setting is for older detectors, whose windows were 1.5 s.
    static func quietLimit(_ detector: DetectorState?, _ settings: AlertSettings) -> Int {
        if let own = detector?.quietAlarmWindows, own > 0 { return own }
        return settings.receiverQuietWindows
    }

    static func chain(_ t: LinkTracker, now: Double, hasCredentials: Bool,
                      quietWindows: Int = quietAlarmWindows, bluetooth: BluetoothView? = nil) -> [ChainItem] {
        var items = serverChain(t, now: now, hasCredentials: hasCredentials, quietWindows: quietWindows)
        guard let b = bluetooth, items[1].level != .ok else { return items }
        // The server has nothing fresh from the car, Bluetooth has: the car computer is there, over Bluetooth.
        if items[0].level != .ok {
            items[0].level = .idle
            items[0].detail = hasCredentials ? "Not reachable; Bluetooth instead" : "Not set up; Bluetooth instead"
        }
        items[1] = ChainItem(id: "pi", name: "Pi", level: .ok, detail: "Over Bluetooth")
        items[2] = detectorItem(b.detector)
        let active = b.detector.state == "running" || b.detector.state == "degraded"
        if !active {
            items[3] = ChainItem(id: "receiver", name: "Receiver", level: .idle, detail: "Closed")
        } else if (b.detector.quietWindows ?? 0) >= quietWindows {
            items[3] = ChainItem(id: "receiver", name: "Receiver", level: .bad, detail: "Hears nothing")
        } else {
            items[3] = ChainItem(id: "receiver", name: "Receiver", level: .ok, detail: "No fault")
        }
        return items
    }

    static func detectorItem(_ d: DetectorState) -> ChainItem {
        var item = ChainItem(id: "detector", name: "Detector", level: .idle, detail: "Unknown")
        switch d.state {
        case "running": item.level = .ok; item.detail = "Running"
        case "degraded": item.level = .warn; item.detail = "Degraded"
        case "starting": item.detail = "Starting"
        case "stale": item.level = .bad; item.detail = "Not responding"
        case "stopped":
            let stoppedByItself = !(d.reasons ?? []).isEmpty
            item.level = stoppedByItself ? .bad : .idle
            item.detail = stoppedByItself ? "Stopped by itself" : "Stopped"
        default: item.detail = d.state
        }
        return item
    }

    private static func serverChain(_ t: LinkTracker, now: Double, hasCredentials: Bool, quietWindows: Int) -> [ChainItem] {
        var server = ChainItem(id: "server", name: "Server", level: .bad, detail: "Not reachable")
        var pi = ChainItem(id: "pi", name: "Pi", level: .idle, detail: "Unknown")
        var detector = ChainItem(id: "detector", name: "Detector", level: .idle, detail: "Unknown")
        var receiver = ChainItem(id: "receiver", name: "Receiver", level: .idle, detail: "Unknown")
        if !hasCredentials { server.level = .idle; server.detail = "Not set up" }
        if t.authFailed { server.detail = "Link refused" }
        if let f = t.frame, !t.authFailed, t.sinceRx(now) <= LinkTracker.linkLost {
            server.level = .ok
            server.detail = "Connected"
            let carAge = t.carAge(now)
            if f.message.serverAge == nil || f.message.state == nil {
                pi.level = .bad; pi.detail = "Never heard"
            } else if !t.clockReady(now) {
                pi.detail = "Checking"
            } else if carAge > LinkTracker.carStale {
                pi.level = .bad; pi.detail = "Silent \(Format.ago(carAge))"
            } else if let d = f.message.state?.detector {
                pi.level = .ok; pi.detail = "Connected"
                detector = detectorItem(d)
                if t.detectorActive {
                    if (d.quietWindows ?? 0) >= quietWindows {
                        receiver.level = .bad
                        receiver.detail = "Hears nothing"
                    } else {
                        receiver.level = .ok
                        receiver.detail = "No fault"
                    }
                } else {
                    receiver.detail = "Closed"
                }
            }
        }
        return [server, pi, detector, receiver]
    }
}

extension String {
    func ifEmpty(_ other: String) -> String { isEmpty ? other : self }
}
