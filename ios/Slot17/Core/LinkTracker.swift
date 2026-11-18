import Foundation

/// A monotonic clock that keeps counting while the phone sleeps, so ages only ever overstate.
enum Mono {
    static func now() -> Double {
        var t = timespec()
        clock_gettime(CLOCK_MONOTONIC, &t)
        return Double(t.tv_sec) + Double(t.tv_nsec) / 1e9
    }
}

/// One accepted message, with what is needed to age it later.
final class Frame {
    let message: ServerMessage
    var ageFloor: Double
    var acceptedAt: Double

    init(_ message: ServerMessage, ageFloor: Double, acceptedAt: Double) {
        self.message = message
        self.ageFloor = ageFloor
        self.acceptedAt = acceptedAt
    }
}

/// Ordering and timing of server messages. A port of the web page's accept/elapsed/live logic:
/// ages are upper bounds, so a slow clock sample may suppress a sound but never freshen data.
final class LinkTracker {
    static let linkLost = 6.0
    static let carStale = 8.0
    static let clockTTL = 30.0
    static let clockRefresh = 10.0
    static let reopenAfter = 9.0
    static let clearAfter = 20.0

    struct ClockSync { let boot: String; let upper: Double; let at: Double }
    enum Acceptance { case refused, repeated, fresh, freshAndNewScope }

    private(set) var frame: Frame?
    private(set) var rxAt = 0.0
    private(set) var clock: ClockSync?
    /// The phone's last timed request to the server, there and back, in seconds.
    private(set) var lastRoundTrip: Double?
    private(set) var scope: String?
    var authFailed = false
    var demo = false
    private var boot: String?
    private var lastSeq = -1
    private var lastStamp = -Double.infinity
    private var retired = Set<String>()

    func reset() {
        frame = nil; rxAt = 0; clock = nil; scope = nil; boot = nil
        lastSeq = -1; lastStamp = -Double.infinity; retired.removeAll(); authFailed = false
    }

    func accept(_ m: ServerMessage, now: Double) -> Acceptance {
        var newScope = false
        if m.boot != boot {
            if retired.contains(m.boot) { return .refused }
            if boot != nil && clock?.boot != m.boot && !demo { return .refused }
            if let old = boot { retired.insert(old) }
            boot = m.boot; lastSeq = -1; lastStamp = -Double.infinity
            newScope = true
        }
        if m.seq < lastSeq || m.serverMono <= lastStamp { return .refused }
        let fresh = m.seq > lastSeq
        let thisScope = "\(m.state?.source ?? "")/\(m.state?.session ?? "")"
        if scope != thisScope { scope = thisScope; newScope = true }
        let floor = (!fresh && frame != nil && clockReady(now)) ? elapsed(frame!, now) : 0
        lastStamp = m.serverMono
        lastSeq = m.seq
        frame = Frame(m, ageFloor: floor, acceptedAt: now)
        rxAt = now
        authFailed = false
        if !fresh { return .repeated }
        return newScope ? .freshAndNewScope : .fresh
    }

    /// A timed request gives an upper bound on the server-to-phone clock offset.
    @discardableResult
    func applyProbe(_ m: ServerMessage, begin: Double, end: Double) -> Acceptance {
        guard !retired.contains(m.boot), end - begin <= 4 else { return .refused }
        let floor = (frame != nil && clockReady(end)) ? elapsed(frame!, end) : 0
        clock = ClockSync(boot: m.boot, upper: m.serverMono - begin, at: end)
        lastRoundTrip = end - begin
        if let f = frame, f.message.boot == m.boot {
            f.ageFloor = floor
            f.acceptedAt = end
        }
        return accept(m, now: end)
    }

    func clockReady(_ now: Double) -> Bool {
        if demo { return true }
        guard let c = clock, c.boot == boot else { return false }
        return now - c.at <= Self.clockTTL
    }

    func elapsed(_ f: Frame, _ now: Double) -> Double {
        guard clockReady(now) else { return .infinity }
        let m = f.message
        let transit: Double
        if demo {
            transit = now - m.serverMono
        } else if let c = clock {
            transit = max(0, now + c.upper - m.serverMono) + 0.03 + max(0, now - c.at) * 0.001
        } else {
            return .infinity
        }
        return max((m.serverAge ?? 0) + transit, f.ageFloor + (now - f.acceptedAt))
    }

    func sinceRx(_ now: Double) -> Double { frame == nil ? .infinity : now - rxAt }

    func carAge(_ now: Double) -> Double {
        guard let f = frame else { return .infinity }
        return (f.message.state?.snapshotAge ?? 0) + elapsed(f, now)
    }

    var detectorState: String? { frame?.message.state?.detector.state }
    var detectorActive: Bool { detectorState == "running" || detectorState == "degraded" }

    func live(_ now: Double) -> Bool {
        !authFailed && clockReady(now) && frame != nil && sinceRx(now) <= Self.linkLost
            && carAge(now) <= Self.carStale && detectorActive
    }

    /// Matches stay waiting only for the very first clock sample. Anything else discards them.
    func mayHoldMatches(_ now: Double) -> Bool {
        clock == nil && !authFailed && sinceRx(now) <= Self.linkLost && detectorActive
    }
}
