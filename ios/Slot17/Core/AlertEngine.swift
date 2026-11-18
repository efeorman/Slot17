import Foundation

struct AlertSettings: Codable, Equatable {
    var red = 22.0
    var yellow = 12.0
    var sound = true
    var urgent = true
    var repeatNeeded = false
    var antennaWarning = true
    var linkLostTone = true
    var volume = 1.0
    var beepStyle = BeepStyle.mix
    var keepScreenOn = true
    var autoSleepMinutes = 30
    // Rules the server's settings file can change without an app update.
    var urgentLevel = 50.0
    var urgentGap = 1.0
    var anyGap = 6.0
    var channelGap = 30.0
    var maxAge = 10.0
    var quiet = false
    var quietRed = 28.0
    var quietGap = 30.0
    /// The soft call tone: a call pattern from this strength up to the chime level.
    var callLevel = 18.0
    var callGap = 60.0         // per channel
    var callAnyGap = 15.0      // between call tones; never holds back a chime or an urgent beep
    var linkLostAfter = 30.0
    var receiverQuietWindows = 40    // 60 s of 1.5 s detector windows, as the Pi counts them
    var receiverToneRepeat = 300.0
    var summaryWindow = 120.0
    var trackEvery = 10.0
    var recordLocation = true
    /// The Live Activity on the Lock Screen and the CarPlay Dashboard.
    var liveActivity = true
    /// The car computer's faults as notifications from this app (only while the app runs).
    var faultNotices = false
    /// Settings the operator changed in the app; the server's file leaves these alone.
    var overrides: Set<String> = []

    /// Quiet mode raises the chime level and spaces chimes out. Urgent beeps are never changed by it.
    var effectiveRed: Double { quiet ? max(red, quietRed) : red }
    var effectiveAnyGap: Double { quiet ? max(anyGap, quietGap) : anyGap }
    var effectiveChannelGap: Double { quiet ? max(channelGap, quietGap) : channelGap }

    /// Back to the app's own rules, again except for anything the operator set in the app.
    mutating func restoreBuiltInRules() {
        let d = AlertSettings()
        func set<T>(_ key: String, _ path: WritableKeyPath<AlertSettings, T>) {
            if !overrides.contains(key) { self[keyPath: path] = d[keyPath: path] }
        }
        set("red", \.red)
        set("yellow", \.yellow)
        set("urgentLevel", \.urgentLevel)
        set("urgentGap", \.urgentGap)
        set("anyGap", \.anyGap)
        set("channelGap", \.channelGap)
        set("maxAge", \.maxAge)
        set("quietRed", \.quietRed)
        set("quietGap", \.quietGap)
        set("callLevel", \.callLevel)
        set("callGap", \.callGap)
        set("callAnyGap", \.callAnyGap)
        set("linkLostAfter", \.linkLostAfter)
        set("receiverQuietWindows", \.receiverQuietWindows)
        set("receiverToneRepeat", \.receiverToneRepeat)
        set("summaryWindow", \.summaryWindow)
        set("trackEvery", \.trackEvery)
        if yellow > red { yellow = red }
    }

    /// Takes the server's values, except for anything the operator set in the app.
    mutating func apply(_ config: RemoteConfig) {
        func set<T>(_ key: String, _ value: T?, _ path: WritableKeyPath<AlertSettings, T>) {
            if let value, !overrides.contains(key) { self[keyPath: path] = value }
        }
        let r = config.rules
        set("red", r?.redDB, \.red)
        set("yellow", r?.yellowDB, \.yellow)
        set("urgentLevel", r?.urgentDB, \.urgentLevel)
        set("urgentGap", r?.urgentGapS, \.urgentGap)
        set("anyGap", r?.chimeGapAnyS, \.anyGap)
        set("channelGap", r?.chimeGapChannelS, \.channelGap)
        set("maxAge", r?.maxAgeS, \.maxAge)
        set("quietRed", r?.quietRedDB, \.quietRed)
        set("quietGap", r?.quietGapS, \.quietGap)
        set("callLevel", r?.callDB, \.callLevel)
        set("callGap", r?.callGapChannelS, \.callGap)
        set("callAnyGap", r?.callGapAnyS, \.callAnyGap)
        let w = config.warnings
        set("linkLostAfter", w?.linkLostAfterS, \.linkLostAfter)
        set("receiverQuietWindows", w?.receiverQuietWindows, \.receiverQuietWindows)
        set("receiverToneRepeat", w?.receiverToneRepeatS, \.receiverToneRepeat)
        set("summaryWindow", config.marks?.summaryWindowS, \.summaryWindow)
        set("trackEvery", config.location?.trackEveryS, \.trackEvery)
        if yellow > red { yellow = red }
    }

    /// The names of settings that differ, used to remember what the operator changed by hand.
    static func changedKeys(_ a: AlertSettings, _ b: AlertSettings) -> Set<String> {
        var keys: Set<String> = []
        if a.red != b.red { keys.insert("red") }
        if a.yellow != b.yellow { keys.insert("yellow") }
        if a.quietRed != b.quietRed { keys.insert("quietRed") }
        if a.quietGap != b.quietGap { keys.insert("quietGap") }
        return keys
    }
}

extension AlertSettings {
    /// Settings saved by an older version lack the new fields; those keep their defaults.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = AlertSettings()
        func get<T: Decodable>(_ key: CodingKeys, _ fallback: T) throws -> T {
            try c.decodeIfPresent(T.self, forKey: key) ?? fallback
        }
        red = try get(.red, d.red); yellow = try get(.yellow, d.yellow); sound = try get(.sound, d.sound)
        urgent = try get(.urgent, d.urgent); repeatNeeded = try get(.repeatNeeded, d.repeatNeeded)
        antennaWarning = try get(.antennaWarning, d.antennaWarning); linkLostTone = try get(.linkLostTone, d.linkLostTone)
        volume = try get(.volume, d.volume); beepStyle = try get(.beepStyle, d.beepStyle)
        keepScreenOn = try get(.keepScreenOn, d.keepScreenOn); autoSleepMinutes = try get(.autoSleepMinutes, d.autoSleepMinutes)
        urgentLevel = try get(.urgentLevel, d.urgentLevel); urgentGap = try get(.urgentGap, d.urgentGap)
        anyGap = try get(.anyGap, d.anyGap); channelGap = try get(.channelGap, d.channelGap)
        maxAge = try get(.maxAge, d.maxAge); quiet = try get(.quiet, d.quiet)
        quietRed = try get(.quietRed, d.quietRed); quietGap = try get(.quietGap, d.quietGap)
        linkLostAfter = try get(.linkLostAfter, d.linkLostAfter)
        receiverQuietWindows = try get(.receiverQuietWindows, d.receiverQuietWindows)
        receiverToneRepeat = try get(.receiverToneRepeat, d.receiverToneRepeat)
        summaryWindow = try get(.summaryWindow, d.summaryWindow); trackEvery = try get(.trackEvery, d.trackEvery)
        recordLocation = try get(.recordLocation, d.recordLocation); overrides = try get(.overrides, d.overrides)
        liveActivity = try get(.liveActivity, d.liveActivity)
        faultNotices = try get(.faultNotices, d.faultNotices)
        callLevel = try get(.callLevel, d.callLevel); callGap = try get(.callGap, d.callGap)
        callAnyGap = try get(.callAnyGap, d.callAnyGap)
    }
}

enum BeepStyle: String, Codable, CaseIterable, Identifiable {
    case mix = "Over music"
    case duck = "Lower music"
    case prompt = "Voice prompt"
    var id: String { rawValue }
}

enum SoundKind: String, Codable {
    /// `call`: soft, for a radio in a call below the chime level.
    case urgent, chime, call, warning, linkLost
    /// A short blip: the widget's tap was recorded. Not an alert.
    case marked
}

/// What happened with one recognised burst, and why.
struct AlertDecision: Equatable {
    enum Outcome: Equatable {
        case played(SoundKind)
        case silent(String)
    }
    let id: String
    let channel: Int
    let strength: Double?
    let age: Double
    let outcome: Outcome
    var rule: String? = nil
}

/// The web page's sound rule, moved here so it can run with the screen locked.
///
/// Strength alone decides, and it is not a distance or a vehicle test. A new match of at least
/// the urgent level plays the urgent beep with a short gap, and each one gets its own beep with no
/// hold timer. A new red match chimes at most once per channel gap and once per overall gap. All
/// of these come from `AlertSettings`, which the server's settings file can change. A sound that
/// cannot play now is dropped, never queued to play late.
final class AlertEngine {
    private static let seenLimit = 400
    private static let waitingLimit = 20

    struct Waiting {
        let match: MatchRow
        let frame: Frame?
        let channels: [ChannelRow]
        let scope: String?
    }

    /// Everything the rule depends on at this moment.
    struct Context {
        var now: Double
        var live: Bool
        var mayHold: Bool
        var scope: String?
        var settings: AlertSettings
        var armed: Bool
        var audioProblem: String?
        var mutedUntil: Double
        var age: (Waiting) -> Double
        var play: (SoundKind) -> String?
    }

    private(set) var lastSilenceReason = ""
    private(set) var lastDelay: Double?
    private var waiting: [Waiting] = []
    private var seen = Set<String>()
    private var seenOrder: [String] = []
    private var lastBeep: [Int: Double] = [:]
    private var lastAny = -Double.greatestFiniteMagnitude
    private var lastUrgent = -Double.greatestFiniteMagnitude
    private var lastCall: [Int: Double] = [:]
    private var lastCallAny = -Double.greatestFiniteMagnitude

    var waitingCount: Int { waiting.count }

    func resetContext() {
        waiting.removeAll()
        lastDelay = nil
    }

    func clearWaiting() { waiting.removeAll() }

    func ingest(_ matches: [MatchRow], frame: Frame?, channels: [ChannelRow], scope: String?) {
        for match in matches where !seen.contains(match.id) {
            remember(match.id)
            waiting.append(Waiting(match: match, frame: frame, channels: channels, scope: scope))
            if waiting.count > Self.waitingLimit { waiting.removeFirst() }
        }
    }

    private func remember(_ id: String) {
        seen.insert(id)
        seenOrder.append(id)
        if seenOrder.count > Self.seenLimit { seen.remove(seenOrder.removeFirst()) }
    }

    private func isUrgent(_ strength: Double?, _ s: AlertSettings) -> Bool {
        guard s.urgent, let db = strength else { return false }
        return db >= s.urgentLevel
    }

    func flush(_ c: Context) -> [AlertDecision] {
        guard c.live else {
            if !c.mayHold { waiting.removeAll() }
            return []
        }
        // Urgent first: an ordinary chime in the same push must not use up the 1 s gap.
        let batch = waiting.enumerated().sorted { a, b in
            let ua = isUrgent(a.element.match.strength, c.settings)
            let ub = isUrgent(b.element.match.strength, c.settings)
            return ua == ub ? a.offset < b.offset : ua
        }.map(\.element)
        waiting.removeAll()
        return batch.map { decide($0, c) }
    }

    private func decide(_ p: Waiting, _ c: Context) -> AlertDecision {
        let s = c.settings
        let age = c.age(p)
        lastDelay = age
        func finish(_ outcome: AlertDecision.Outcome) -> AlertDecision {
            if case .silent(let why) = outcome { lastSilenceReason = why } else { lastSilenceReason = "" }
            return AlertDecision(id: p.match.id, channel: p.match.ch, strength: p.match.strength, age: age, outcome: outcome,
                                 rule: p.match.rule)
        }
        func silent(_ why: String) -> AlertDecision { finish(.silent(why)) }

        if p.scope != c.scope { return silent("the link or the detector was not live") }
        if s.repeatNeeded {
            let row = p.channels.first { $0.ch == p.match.ch }
            if (row?.hits60 ?? 0) < 2 { return silent("“repeat needed” is on and this channel matched once") }
        }
        guard let db = p.match.strength else { return silent("the match had no usable strength") }
        let urgent = isUrgent(db, s)
        // A radio in a call below the chime level gets the soft call tone instead of silence.
        let callTier = !urgent && p.match.call == true && db >= s.callLevel && db < s.effectiveRed
        // The red level and Quiet mode govern chimes only; an urgent beep is never held back by them.
        if !urgent && !callTier && db < s.effectiveRed {
            return silent(String(format: "%.1f dB is below the %.0f dB red level%@", db, s.effectiveRed, s.quiet ? " (Quiet mode)" : ""))
        }
        if age > s.maxAge { return silent(String(format: "it reached the phone %.1f s old (limit %.0f s)", age, s.maxAge)) }
        if let problem = c.audioProblem { return silent(problem) }
        if c.now < c.mutedUntil { return silent("muted by the speaker button") }
        if !s.sound { return silent("sound is switched off") }
        if !c.armed { return silent("sound is not armed") }

        let kind: SoundKind
        if urgent {
            // Only another urgent beep holds this back. On 8 November a 51 dB signal stayed silent
            // because a weak chime had played 14 ms before it, in the previous push.
            if c.now - lastUrgent < s.urgentGap {
                return silent(String(format: "another urgent beep played under %.0f s ago", s.urgentGap))
            }
            kind = .urgent
        } else if callTier {
            if c.now - (lastCall[p.match.ch] ?? -Double.greatestFiniteMagnitude) < s.callGap {
                return silent(String(format: "this channel's call tone played under %.0f s ago", s.callGap))
            }
            if c.now - lastCallAny < s.callAnyGap {
                return silent(String(format: "another call tone played under %.0f s ago", s.callAnyGap))
            }
            if c.now - lastAny < 1 { return silent("another sound played under 1 s ago") }
            if let refused = c.play(.call) { return silent(refused) }
            lastCall[p.match.ch] = c.now
            lastCallAny = c.now
            return finish(.played(.call))
        } else {
            if c.now - (lastBeep[p.match.ch] ?? -Double.greatestFiniteMagnitude) < s.effectiveChannelGap {
                return silent(String(format: "this channel chimed under %.0f s ago", s.effectiveChannelGap))
            }
            if c.now - lastAny < s.effectiveAnyGap {
                return silent(String(format: "another chime played under %.0f s ago", s.effectiveAnyGap))
            }
            kind = .chime
        }
        if let refused = c.play(kind) { return silent(refused) }
        lastBeep[p.match.ch] = c.now
        lastAny = c.now
        if urgent { lastUrgent = c.now }
        return finish(.played(kind))
    }
}
