import Foundation

/// Turns what the app knows into the Live Activity's state, using the same meanings as the meters.
enum LiveTileBuilder {
    struct Input {
        var linkLost: Bool
        var detectorRunning: Bool
        var receiverQuiet: Bool
        var top: MeterRow?
        var lastAlert: AlertRecord?
        var lastMark: Sighting? = nil
        var settings: AlertSettings
        var style: DisplayStyle
        var demo: Bool
        var now: Date
        /// When the top channel's burst was heard, from the unrounded age (the meters round theirs).
        var topHeardAt: Date? = nil
        /// The Pi's id for that burst, when it sends one.
        var topBurstID: String? = nil
        /// What was shown last, so the same burst keeps the same time despite small clock jitter.
        var previous: LiveTileState? = nil
        /// The word about the car computer's software, while it is news.
        var software: LiveTileState.Software? = nil
        /// Channels heard in the last minute besides the top one.
        var othersActive: Int = 0
        /// How old the car's newest data is, in seconds.
        var carAge: Double? = nil
    }

    /// Car data older than this (but not yet lost) is called late on the tile; the car pushes at least
    /// every 2 s, so up to about 3 s is normal. Once late, it is called late until below `slowClears`.
    static let slowAfter: TimeInterval = 5
    static let slowClears: TimeInterval = 3.5
    /// The last channel heard is shown while listening for this long.
    static let lastHeardFor: TimeInterval = 30 * 60
    /// A mark names the channel heard within this long of the tap.
    static let markContextWithin: TimeInterval = 120

    /// A beep older than this is not shown on the tile any more.
    static let alertShownFor: TimeInterval = 15 * 60
    /// How long a marked sighting answers on the tile.
    static let markShownFor: TimeInterval = 30

    static func make(_ i: Input) -> LiveTileState {
        var state = LiveTileState.placeholder
        state.colours = [i.style.weak, i.style.silent, i.style.chime, i.style.strongOutline]
        state.badge = i.style.strongBadge
        state.demo = i.demo
        if let alert = i.lastAlert, let sound = alert.sound, sound == .urgent || sound == .chime,
           i.now.timeIntervalSince(alert.at) < alertShownFor {
            state.lastAlert = .init(urgent: sound == .urgent, channel: alert.channel,
                                    strength: alert.strength.map { Int($0.rounded()) }, at: alert.at)
        }
        if let mark = i.lastMark, i.now.timeIntervalSince(mark.seenAt) < markShownFor {
            state.mark = .init(title: mark.label.title, at: mark.seenAt)
        }
        // A beep in the last minute keeps its line: it matters more than news about software.
        if let software = i.software, i.lastAlert.map({ i.now.timeIntervalSince($0.at) >= 60 }) ?? true {
            state.software = software
        }
        if i.linkLost {
            state.phase = .linkLost
        } else if !i.detectorRunning {
            state.phase = .stopped
        } else if i.receiverQuiet {
            state.phase = .receiverQuiet
        } else if let top = i.top {
            state.phase = .signal
            state.channel = top.ch
            state.strength = top.strength.map { Int($0.rounded()) }
            state.level = Meters.level(top.strength, urgent: i.settings.urgentLevel)
            state.frequent = top.frequent
            // Whole seconds. The same burst keeps the time already shown, so clock jitter never causes
            // an update; a new burst always gets its own time. The Pi's burst id decides which is which;
            // without one, within 1.5 s on the same channel counts as the same burst.
            let exact = i.topHeardAt ?? i.now.addingTimeInterval(-top.age)
            let rounded = Date(timeIntervalSince1970: exact.timeIntervalSince1970.rounded())
            state.burstID = i.topBurstID
            var sameBurst = false
            if let previous = i.previous, previous.channel == top.ch, let shown = previous.heardAt {
                if let old = previous.burstID, let new = i.topBurstID {
                    sameBurst = old == new
                } else {
                    sameBurst = abs(shown.timeIntervalSince(rounded)) < 1.5
                }
                if sameBurst { state.heardAt = shown }
            }
            if !sameBurst { state.heardAt = rounded }
            if Meters.isStrong(top.strength, i.settings) {
                state.tone = .strong
            } else {
                switch Meters.colour(top.strength, i.settings) {
                case .red: state.tone = .chime
                case .yellow: state.tone = .silent
                case .weak: state.tone = .weak
                case .grey: state.tone = .none
                }
            }
            state.call = top.call ? true : nil
            state.rising = top.rising ? true : nil
            state.others = i.othersActive > 0 ? i.othersActive : nil
        } else {
            state.phase = .listening
            if let previous = i.previous {
                if previous.phase == .signal, let channel = previous.channel, let at = previous.heardAt {
                    state.lastHeard = .init(channel: channel, at: at)
                } else {
                    state.lastHeard = previous.lastHeard
                }
            }
            if let last = state.lastHeard, i.now.timeIntervalSince(last.at) > lastHeardFor { state.lastHeard = nil }
        }
        if state.phase == .signal || state.phase == .listening, let age = i.carAge,
           age > (i.previous?.slow == true ? slowClears : slowAfter) {
            state.slow = true
        }
        if var mark = state.mark {
            if let old = i.previous?.mark, old.title == mark.title, old.at == mark.at {
                mark.context = old.context
            } else {
                mark.context = markContext(mark.at, state)
            }
            state.mark = mark
        }
        return state
    }

    /// "L60 12 s before": the channel heard nearest a tap, if any within two minutes.
    static func markContext(_ at: Date, _ state: LiveTileState) -> String? {
        var heard: LiveTileState.Heard?
        if state.phase == .signal, let channel = state.channel, let time = state.heardAt {
            heard = .init(channel: channel, at: time)
        } else {
            heard = state.lastHeard
        }
        guard let heard else { return nil }
        let gap = at.timeIntervalSince(heard.at)
        guard abs(gap) <= markContextWithin else { return nil }
        let seconds = Int(abs(gap).rounded())
        return "L\(heard.channel) \(seconds) s \(gap >= 0 ? "before" : "after")"
    }
}

/// What the tile says about the car computer's software, and for how long once it appears.
enum SoftwareNote {
    static func make(_ update: UpdateState?) -> (software: LiveTileState.Software, shownFor: TimeInterval)? {
        guard let update else { return nil }
        let commit = String((update.commit ?? "").prefix(7))
        switch update.state {
        case "installed":
            return (.init(text: commit.isEmpty ? "Software updated" : "Updated to \(commit)", problem: false), 300)
        case "staged":
            return (.init(text: "Update ready for next start", problem: false), 120)
        case "rejected":
            return (.init(text: "Update failed its checks", problem: true), 300)
        case "error":
            return (.init(text: "Update problem", problem: true), 300)
        default:
            return nil
        }
    }
}

/// When to send an update: at once for what matters, otherwise sparingly. The tile's clocks count
/// up on the device by themselves, so time passing never needs an update.
struct LiveTilePolicy {
    /// Ordinary changes (another dB, another channel, another burst) at most this often. Apple asks
    /// for "only the most significant states" on CarPlay; the server can change it (`tile_gap_s`).
    var minInterval: TimeInterval = 10
    /// A periodic update just to move the stale date on, so the tile can say "not updating" honestly.
    static let keepAlive: TimeInterval = 20
    /// The tile shows "Not updating" if nothing arrives for this long.
    static let staleAfter: TimeInterval = 45

    private(set) var sent: LiveTileState?
    private(set) var sentAt: TimeInterval = -.infinity

    /// Changes that may not wait: a new beep, another phase, STRONG on or off, a new sighting, a louder
    /// colour, a call starting, car data turning late.
    static func isUrgent(_ old: LiveTileState?, _ new: LiveTileState) -> Bool {
        guard let old else { return true }
        return old.phase != new.phase || old.lastAlert != new.lastAlert
            || (old.tone == .strong) != (new.tone == .strong) || old.demo != new.demo
            || (new.mark != nil && old.mark != new.mark) || old.software != new.software
            || (new.phase == .signal && new.tone.rank > old.tone.rank)
            || (new.call == true && old.call != true)
            || (new.slow == true && old.slow != true)
    }

    func shouldSend(_ new: LiveTileState, now: TimeInterval) -> Bool {
        if Self.isUrgent(sent, new) { return true }
        if new != sent { return now - sentAt >= minInterval }
        return now - sentAt >= Self.keepAlive
    }

    mutating func didSend(_ state: LiveTileState, now: TimeInterval) {
        sent = state
        sentAt = now
    }

    mutating func reset() {
        sent = nil
        sentAt = -.infinity
    }
}

extension LiveTileState.Tone {
    /// Louder colours rank higher; a step up is sent at once.
    var rank: Int {
        switch self {
        case .none: return 0
        case .weak: return 1
        case .silent: return 2
        case .chime: return 3
        case .strong: return 4
        }
    }
}
