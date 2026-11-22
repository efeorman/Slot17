import CryptoKit
import Foundation

/// The car computer's Bluetooth link, version 1: the frames `p0/bluetooth_frames.py` sends, decoded and checked.
/// `shared/parity/ble-frames.json` holds frames that this, the Android app and the car computer must all read the
/// same way. docs/design/BLUETOOTH.md describes the whole link.
enum BluetoothFormat {
    static let serviceUUID = "f9cb0001-aa93-4349-a576-0e70a736266f"
    static let framesUUID = "f9cb0002-aa93-4349-a576-0e70a736266f"
    static let infoUUID = "f9cb0003-aa93-4349-a576-0e70a736266f"
    static let controlUUID = "f9cb0004-aa93-4349-a576-0e70a736266f"
    static let version: UInt8 = 1
    static let headerBytes = 10
    static let tagBytes = 8
    static let maxFrame = 180
    static let keyBytes = 32
    private static let statusBytes = 20
    private static let matchBytes = 15
    private static let channelBytes = 17
    private static let detectorStates = ["stopped", "starting", "running", "degraded", "stale"]
    private static let rules: [String?] = [nil, "standard", "weak", "coherent"]
    private static let activities: [String?] = [nil, "active", "intermittent"]

    struct FrameError: Error, Equatable { let why: String }

    /// What a phone can ask over the link itself. Switching on needs the server: an off link hears nothing.
    enum Action: UInt8 { case off = 1, rename = 2 }
    private static let controlKind: UInt8 = 16

    /// A checked frame. Matches carry the key every route shares, so a match plays once.
    enum Frame {
        case status(boot: UInt32, counter: UInt32, detector: DetectorState)
        case matches(boot: UInt32, counter: UInt32, matches: [MatchRow])
        case channels(boot: UInt32, counter: UInt32, channels: [ChannelRow])
        case reply(boot: UInt32, counter: UInt32, action: Action?, ok: Bool, text: String)

        var boot: UInt32 {
            switch self {
            case .status(let b, _, _), .matches(let b, _, _), .channels(let b, _, _), .reply(let b, _, _, _, _): return b
            }
        }
        var counter: UInt32 {
            switch self {
            case .status(_, let c, _), .matches(_, let c, _), .channels(_, let c, _), .reply(_, let c, _, _, _): return c
            }
        }
    }

    /// A control frame for the car computer: `boot` and `counter` are those of the newest frame from it, and each
    /// counter works once, so a recording of the command cannot be played back later.
    static func control(key: Data, boot: UInt32, counter: UInt32, action: Action, name: String = "") -> Data {
        let words = action == .rename ? Array(name.utf8) : []
        var head: [UInt8] = [version, controlKind]
        for value in [boot, counter] { head += [UInt8(value & 0xFF), UInt8(value >> 8 & 0xFF), UInt8(value >> 16 & 0xFF), UInt8(value >> 24)] }
        head += [action.rawValue, UInt8(words.count)] + words
        let tag = Array(HMAC<SHA256>.authenticationCode(for: head, using: SymmetricKey(data: key))).prefix(tagBytes)
        return Data(head + tag)
    }

    /// Decodes `frame` after checking its tag with `key`.
    static func decode(_ frame: Data, key: Data) throws -> Frame {
        let bytes = [UInt8](frame)
        guard bytes.count >= headerBytes + tagBytes, bytes.count <= maxFrame else { throw FrameError(why: "wrong length") }
        let head = Array(bytes[0..<(bytes.count - tagBytes)])
        let tag = Array(bytes[(bytes.count - tagBytes)...])
        let expected = Array(HMAC<SHA256>.authenticationCode(for: head, using: SymmetricKey(data: key))).prefix(tagBytes)
        guard zip(tag, expected).reduce(UInt8(0), { $0 | ($1.0 ^ $1.1) }) == 0 else {
            throw FrameError(why: "not signed with this key")
        }
        var r = Reader(head)
        let v = r.u8()
        guard v == version else { throw FrameError(why: "version \(v)") }
        let kind = r.u8()
        let boot = r.u32()
        let counter = r.u32()
        let body = head.count - headerBytes
        switch kind {
        case 1:
            guard body == statusBytes else { throw FrameError(why: "status length") }
            let state = Int(r.u8())
            _ = r.u8()
            var d = DetectorState(state: state < detectorStates.count ? detectorStates[state] : "unknown")
            d.windows = Int(r.u32())
            d.hits = Int(r.u32())
            d.quietWindows = Int(r.u16())
            let alarm = Int(r.u16())
            d.quietAlarmWindows = alarm > 0 ? alarm : nil
            d.wholeBandWindows = Int(r.u16())
            d.heartbeatUnchanged = Double(r.u16()) / 10
            d.floorDB = tenths(r.i16())
            return .status(boot: boot, counter: counter, detector: d)
        case 2:
            let rows = try count(&r, body, matchBytes)
            let matches = (0..<rows).map { _ -> MatchRow in
                let key = hex(r.bytes(8))
                let ch = Int(r.u16())
                let strength = tenths(r.i16())
                let age = Double(r.u16()) / 1000
                let rule = Int(r.u8())
                return MatchRow(id: "bt:" + key, ch: ch, strength: strength, age: age,
                                rule: rule < rules.count ? rules[rule] : nil, key: key)
            }
            return .matches(boot: boot, counter: counter, matches: matches)
        case 3:
            let rows = try count(&r, body, channelBytes)
            let channels = (0..<rows).map { _ -> ChannelRow in
                let ch = Int(r.u16())
                let freq = Double(r.u32()) / 1e4
                let strength = tenths(r.i16())
                let age = Double(r.u16()) / 10
                let hits = Int(r.u16())
                let flags = Int(r.u8())
                let burst = hex(r.bytes(4))
                let activity = flags >> 4 < activities.count ? activities[flags >> 4] : nil
                return ChannelRow(ch: ch, freqMHz: freq, strength: strength, age: age, hits60: hits,
                                  approxTime: flags & 4 != 0, activity: activity, burstID: "bt:" + burst,
                                  rising: flags & 1 != 0, call: flags & 2 != 0)
            }
            return .channels(boot: boot, counter: counter, channels: channels)
        case 4:
            guard body >= 3 else { throw FrameError(why: "reply length") }
            let action = Action(rawValue: r.u8())
            let ok = r.u8() == 1
            let n = Int(r.u8())
            guard body == 3 + n else { throw FrameError(why: "reply length") }
            return .reply(boot: boot, counter: counter, action: action, ok: ok, text: String(decoding: r.bytes(n), as: UTF8.self))
        default:
            throw FrameError(why: "kind \(kind)")
        }
    }

    /// A short public name for a key: the first 8 hex digits of its SHA-256.
    static func keyID(_ key: Data) -> String { String(hex(Array(SHA256.hash(data: key))).prefix(8)) }

    private static func count(_ r: inout Reader, _ body: Int, _ size: Int) throws -> Int {
        guard body >= 1 else { throw FrameError(why: "row count") }
        let n = Int(r.u8())
        guard body == 1 + n * size else { throw FrameError(why: "row count") }
        return n
    }

    private static func tenths(_ v: Int16) -> Double? { v == Int16.min ? nil : Double(v) / 10 }

    static func hex<S: Sequence>(_ bytes: S) -> String where S.Element == UInt8 {
        bytes.map { String(format: "%02x", $0) }.joined()
    }

    /// Little-endian reads over a frame whose length was checked first.
    private struct Reader {
        private let b: [UInt8]
        private var i = 0
        init(_ b: [UInt8]) { self.b = b }
        mutating func u8() -> UInt8 { defer { i += 1 }; return b[i] }
        mutating func u16() -> UInt16 { UInt16(u8()) | UInt16(u8()) << 8 }
        mutating func i16() -> Int16 { Int16(bitPattern: u16()) }
        mutating func u32() -> UInt32 { UInt32(u16()) | UInt32(u16()) << 16 }
        mutating func bytes(_ n: Int) -> [UInt8] { defer { i += n }; return Array(b[i..<(i + n)]) }
    }
}

/// The key of a match id: the first 8 bytes of its SHA-256, the same on the car computer and every route.
enum MatchKey {
    static func of(_ id: String) -> String { String(BluetoothFormat.hex(Array(SHA256.hash(data: Data(id.utf8)))).prefix(16)) }
    /// The key the alert engine remembers a match by.
    static func of(_ match: MatchRow) -> String { match.key ?? of(match.id) }
}

/// Which signed frames to take: within a boot only a higher counter, and never an earlier boot again, so a
/// recording of the link replayed later cannot raise an alert.
struct FrameAcceptor {
    private var boot: UInt32?
    private var counter: Int64 = -1
    private var old: [UInt32] = []
    var remember = 32

    mutating func accept(boot: UInt32, counter: UInt32) -> Bool {
        if boot == self.boot {
            if Int64(counter) <= self.counter { return false }
        } else {
            if old.contains(boot) { return false }
            if let previous = self.boot {
                old.append(previous)
                if old.count > remember { old.removeFirst() }
            }
            self.boot = boot
        }
        self.counter = Int64(counter)
        return true
    }
}

/// Which route brought each match first, and by how much: the measured answer to "is Bluetooth faster here".
struct RouteStats {
    enum Route { case bluetooth, server }

    private var first: [String: (Route, Double)] = [:]
    private var order: [String] = []
    private var leads: [Double] = []
    private(set) var bluetoothFirst = 0
    private(set) var serverFirst = 0
    private(set) var bluetoothOnly = 0

    mutating func arrived(_ key: String, _ route: Route, at: Double) {
        guard let seen = first[key] else {
            first[key] = (route, at)
            order.append(key)
            if order.count > 400 { first.removeValue(forKey: order.removeFirst()) }
            return
        }
        guard seen.0 != route else { return }
        first.removeValue(forKey: key)
        if seen.0 == .bluetooth {
            bluetoothFirst += 1
            leads.append(at - seen.1)
            if leads.count > 200 { leads.removeFirst() }
        } else {
            serverFirst += 1
        }
    }

    /// Matches heard over Bluetooth that the server had not delivered after `wait` seconds.
    mutating func settle(now: Double, wait: Double = 60) {
        for (key, value) in first where now - value.1 > wait {
            if value.0 == .bluetooth { bluetoothOnly += 1 }
            first.removeValue(forKey: key)
        }
        order.removeAll { first[$0] == nil }
    }

    /// The median time Bluetooth was ahead of the server, in seconds, or nil before any comparison.
    var medianLead: Double? { leads.isEmpty ? nil : leads.sorted()[leads.count / 2] }
    var compared: Bool { bluetoothFirst + serverFirst + bluetoothOnly > 0 }
}

/// What came over Bluetooth, for the app: the key frames are checked with, the newest detector state and channel
/// rows, and when they arrived. The app's Bluetooth service hands it the bytes; nothing here touches Bluetooth.
struct BluetoothFeed {
    enum Received: Equatable { case matches([String]), state, refused(String), reply(ok: Bool, text: String) }

    var key: Data? { didSet { reset() } }
    private var acceptor = FrameAcceptor()
    private(set) var detector: DetectorState?
    private(set) var channels: [ChannelRow] = []
    private(set) var matches: [MatchRow] = []
    private var statusAt = -Double.infinity
    private var channelsAt = -Double.infinity
    private(set) var lastFrameAt = -Double.infinity
    private(set) var frames = 0
    private(set) var refused = 0
    private var lastBoot: UInt32?
    private var lastCounter: UInt32 = 0

    init(key: Data? = nil) { self.key = key }

    /// Takes one frame. New matches are left in `matches` for the caller, with the keys in the result.
    mutating func receive(_ bytes: Data, now: Double) -> Received {
        matches = []
        guard let key else { return refuse("no key for this car computer yet") }
        let frame: BluetoothFormat.Frame
        do {
            frame = try BluetoothFormat.decode(bytes, key: key)
        } catch let error as BluetoothFormat.FrameError {
            return refuse(error.why)
        } catch {
            return refuse("unreadable frame")
        }
        guard acceptor.accept(boot: frame.boot, counter: frame.counter) else { return refuse("a frame it had already sent") }
        frames += 1
        lastFrameAt = now
        lastBoot = frame.boot
        lastCounter = frame.counter
        switch frame {
        case .reply(_, _, _, let ok, let text):
            return .reply(ok: ok, text: text)
        case .status(_, _, let d):
            detector = d
            statusAt = now
            return .state
        case .channels(_, _, let rows):
            channels = rows
            channelsAt = now
            return .state
        case .matches(_, _, let rows):
            matches = rows
            return .matches(rows.compactMap(\.key))
        }
    }

    private mutating func refuse(_ why: String) -> Received {
        refused += 1
        return .refused(why)
    }

    /// The car is live over Bluetooth: a fresh status that says the detector runs, by LinkTracker's rule.
    func live(_ now: Double) -> Bool {
        now - statusAt <= LinkTracker.carStale && (detector?.state == "running" || detector?.state == "degraded")
    }

    /// Any checked frame lately, whatever the detector says.
    func heard(_ now: Double) -> Bool { now - lastFrameAt <= LinkTracker.carStale }

    /// What the screens show when only Bluetooth brings data: the last server state with Bluetooth's detector and channels.
    func piState(base: PiState?, now: Double) -> PiState? {
        guard let detector else { return nil }
        var state = base ?? PiState(snapshotAge: 0, detector: detector, channels: [], matches: [])
        state.snapshotAge = max(0, now - statusAt)
        state.detector = detector
        state.channels = channels.map { row in
            var aged = row
            aged.age += now - channelsAt
            return aged
        }
        state.matches = []
        return state
    }

    /// A signed control frame for the car computer, or nil before any frame came from it.
    func control(_ action: BluetoothFormat.Action, name: String = "") -> Data? {
        guard let key, let lastBoot else { return nil }
        return BluetoothFormat.control(key: key, boot: lastBoot, counter: lastCounter, action: action, name: name)
    }

    mutating func reset() {
        lastBoot = nil
        acceptor = FrameAcceptor()
        detector = nil
        channels = []
        statusAt = -.infinity
        channelsAt = -.infinity
    }
}

/// The name phones show for a car computer: the rule p0/bluetooth_config.py and the server use.
enum BluetoothName {
    static let defaultName = "Slot17"
    static let maxLength = 20

    static func valid(_ name: String) -> Bool {
        guard (1...maxLength).contains(name.count), name == name.trimmingCharacters(in: .whitespaces),
              let first = name.unicodeScalars.first, first.isASCII, CharacterSet.alphanumerics.contains(first) else { return false }
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-")
        return name.unicodeScalars.allSatisfy { allowed.contains($0) }
    }
}

/// The car computer's Bluetooth state in plain words.
enum BluetoothWords {
    static func car(_ b: BluetoothState?) -> String {
        guard let b else { return "The car computer has not reported its Bluetooth yet" }
        if !b.on { return "Off" }
        switch b.state {
        case "connected": return "A phone is connected"
        case "advertising": return "On, waiting for a phone"
        case "starting": return "Starting"
        case "unavailable": return "Not available: \(b.error ?? "no Bluetooth adapter")"
        case "error": return "Problem: \(b.error ?? "unknown")"
        default: return b.state
        }
    }
}

/// Bytes from hex digits, or nil when they are not hex.
func hexData(_ text: String) -> Data? {
    guard text.count % 2 == 0 else { return nil }
    var out = Data(capacity: text.count / 2)
    var index = text.startIndex
    while index < text.endIndex {
        let next = text.index(index, offsetBy: 2)
        guard let byte = UInt8(text[index..<next], radix: 16) else { return nil }
        out.append(byte)
        index = next
    }
    return out
}
