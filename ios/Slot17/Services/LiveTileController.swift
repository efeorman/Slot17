import ActivityKit
import Foundation
import UIKit

/// Starts, updates and ends the Live Activity that iOS 26 also shows on the CarPlay Dashboard.
///
/// Rules from Apple's ActivityKit documentation that shape this:
/// - an activity can only be started while the app is in the foreground;
/// - the app may update it from the background, and updates to an ended activity are ignored;
/// - state is limited to 4 KB; an activity lasts at most 8 hours, then iOS ends it;
/// - a stale date makes the tile say it is out of date if updates stop arriving.
/// Developers report that an app kept running only by background audio cannot update its activity.
/// This app also runs location in the background; the log records every update so one drive shows
/// whether they arrive. Used only from the main thread, like AppModel.
final class LiveTileController {
    private var activity: Activity<SlotActivityAttributes>?
    private var policy = LiveTilePolicy()
    private var inFlight = false
    private var queued: LiveTileState?
    private var countThisMinute = 0
    private var backgroundThisMinute = 0
    private var changedThisMinute = 0
    private var changedBackgroundThisMinute = 0
    private var deliveredThisMinute = 0
    private var deliveredBackgroundThisMinute = 0
    private var queuedChanged = false
    /// Bumped at every start and end, so a slow update for an old activity cannot touch a new one.
    private var generation = 0
    /// Identifies the update in flight; one that never returns must not block the ones after it.
    private var sendToken = 0
    private var sendStartedAt = -Double.infinity
    private static let sendGivesUpAfter: TimeInterval = 5
    private var minuteStart = Date()
    /// The number on the newest update sent, and on the newest content iOS reported back.
    private var revision = 0
    private var observedRevision: Int?
    private var lastStartAttempt = -Double.infinity
    private let note: (String) -> Void
    private(set) var status = "Off"

    init(note: @escaping (String) -> Void) {
        self.note = note
        // A previous run's activity (the app was closed by iOS) is taken over, never duplicated.
        activity = Activity<SlotActivityAttributes>.activities.first { $0.activityState == .active || $0.activityState == .stale }
        for extra in Activity<SlotActivityAttributes>.activities where extra.id != activity?.id {
            Task { await extra.end(nil, dismissalPolicy: .immediate) }
        }
        if let activity {
            revision = activity.content.state.rev ?? 0
            status = "Showing"
            note("live activity taken over from the last run")
            watch()
        }
    }

    /// After the person changed something (switched it on, opened the app), try at once.
    func allowImmediateStart() { lastStartAttempt = -.infinity }

    var minInterval: TimeInterval {
        get { policy.minInterval }
        set { policy.minInterval = newValue }
    }

    /// The last state handed to iOS, for the builder to keep a burst's time steady.
    var lastSent: LiveTileState? { policy.sent }

    var isRunning: Bool { activity.map { $0.activityState == .active || $0.activityState == .stale } ?? false }

    var enabledInSettings: Bool { ActivityAuthorizationInfo().areActivitiesEnabled }

    /// Only from the foreground. Returns quietly if Live Activities are switched off for the app.
    func start(_ state: LiveTileState) {
        guard !isRunning else { return }
        // The extension is built for iOS 18 (the small family CarPlay uses); CarPlay needs iOS 26.
        guard #available(iOS 18, *) else {
            status = "Needs iOS 18 or later"
            return
        }
        guard UIApplication.shared.applicationState == .active else { return }
        // Called every tick while there is none: try again at most every 30 s.
        guard Mono.now() - lastStartAttempt >= 30 else { return }
        lastStartAttempt = Mono.now()
        guard enabledInSettings else {
            status = "Switched off in iPhone Settings › Slot17 › Live Activities"
            return
        }
        do {
            revision = 1
            observedRevision = nil
            var numbered = state
            numbered.rev = revision
            let content = ActivityContent(state: numbered, staleDate: Date().addingTimeInterval(LiveTilePolicy.staleAfter))
            activity = try Activity.request(attributes: SlotActivityAttributes(startedAt: Date()), content: content, pushType: nil)
            generation += 1
            inFlight = false
            queued = nil
            queuedChanged = false
            policy.reset()
            policy.didSend(state, now: Mono.now())
            status = "Showing"
            note("live activity started (Lock Screen and CarPlay Dashboard)")
            watch()
        } catch {
            status = "Could not start: \(error.localizedDescription)"
            note("PROBLEM live activity could not start: \(error.localizedDescription)")
        }
    }

    /// Called every tick; sends only when the policy says so, one update at a time, newest wins.
    func update(_ state: LiveTileState) {
        guard isRunning else { return }
        let now = Mono.now()
        guard policy.shouldSend(state, now: now) else { return }
        let changed = state != policy.sent
        policy.didSend(state, now: now)
        if inFlight, now - sendStartedAt > Self.sendGivesUpAfter {
            note(String(format: "PROBLEM live activity: an update has not returned after %.0f s, moving on", now - sendStartedAt))
            inFlight = false
            // What waited behind the hung update is older than this one, and must not follow it.
            queued = nil
            queuedChanged = false
        }
        if inFlight {
            queued = state
            queuedChanged = queuedChanged || changed
            return
        }
        send(state, changed: changed)
    }

    private func send(_ state: LiveTileState, changed: Bool) {
        guard let activity else { return }
        inFlight = true
        sendToken += 1
        let token = sendToken
        sendStartedAt = Mono.now()
        let background = UIApplication.shared.applicationState != .active
        count(background: background)
        revision += 1
        var numbered = state
        numbered.rev = revision
        let content = ActivityContent(state: numbered, staleDate: Date().addingTimeInterval(LiveTilePolicy.staleAfter),
                                      relevanceScore: state.tone == .strong ? 100 : 50)
        let sentGeneration = generation
        Task { @MainActor in
            await activity.update(content)
            guard sentGeneration == self.generation, token == self.sendToken else { return }
            // update(_:) reports nothing; iOS's contentUpdates (see watch()) show what it took on, though
            // not one event per call and not that the Lock Screen or CarPlay drew it. Reading
            // activity.content back right away proved useless: it lags behind.
            if changed {
                self.changedThisMinute += 1
                if background { self.changedBackgroundThisMinute += 1 }
            }
            self.inFlight = false
            if let next = self.queued {
                let nextChanged = self.queuedChanged
                self.queued = nil
                self.queuedChanged = false
                self.send(next, changed: nextChanged)
            }
        }
    }

    /// Always with a final state that shows no signal, so nothing old can look current. Removed at
    /// once when switched off; otherwise it stays 10 minutes saying the drive ended.
    func end(final state: LiveTileState, reason: String, immediately: Bool, finished: (() -> Void)? = nil) {
        guard let activity else {
            finished?()
            return
        }
        self.activity = nil
        generation += 1
        policy.reset()
        inFlight = false
        queued = nil
        queuedChanged = false
        status = "Off"
        note("live activity ended: \(reason)")
        let content = ActivityContent(state: state, staleDate: nil)
        let dismissal: ActivityUIDismissalPolicy = immediately ? .immediate : .after(Date().addingTimeInterval(10 * 60))
        Task { @MainActor in
            await activity.end(content, dismissalPolicy: dismissal)
            self.note("live activity end finished; \(Self.stillShowing) still showing")
            finished?()
        }
    }

    /// Activities iOS still counts as showing, whatever this controller believes.
    private static var stillShowing: Int {
        Activity<SlotActivityAttributes>.activities.filter { $0.activityState == .active || $0.activityState == .stale }.count
    }

    /// For the moment the app ends: an ordinary end would never finish, so this waits for it, briefly.
    func endAndWait(final state: LiveTileState, reason: String) {
        guard let activity else { return }
        self.activity = nil
        generation += 1
        policy.reset()
        inFlight = false
        queued = nil
        queuedChanged = false
        status = "Off"
        note("live activity ended: \(reason)")
        let content = ActivityContent(state: state, staleDate: nil)
        let done = DispatchSemaphore(value: 0)
        Task.detached {
            await activity.end(content, dismissalPolicy: .immediate)
            done.signal()
        }
        if done.wait(timeout: .now() + 2) == .success {
            note("live activity end finished; \(Self.stillShowing) still showing")
        } else {
            note("PROBLEM live activity did not finish ending within 2 s")
        }
    }

    /// One line a minute: how many updates were sent and how many while in the background.
    private func count(background: Bool) {
        if Date().timeIntervalSince(minuteStart) >= 60 {
            if countThisMinute > 0 {
                note("live activity: \(countThisMinute) updates in the last minute (\(backgroundThisMinute) in the background); "
                     + "\(changedThisMinute) changes sent, \(deliveredThisMinute) observed by iOS; "
                     + "\(changedBackgroundThisMinute) changes sent in the background, \(deliveredBackgroundThisMinute) observed while in the background; "
                     + "newest sent R\(revision), newest reported by iOS R\(observedRevision.map(String.init) ?? "-")")
            }
            countThisMinute = 0
            backgroundThisMinute = 0
            changedThisMinute = 0
            changedBackgroundThisMinute = 0
            deliveredThisMinute = 0
            deliveredBackgroundThisMinute = 0
            minuteStart = Date()
        }
        countThisMinute += 1
        if background { backgroundThisMinute += 1 }
    }

    /// iOS can end the activity (8-hour limit, or the person dismisses it); notice and say so.
    /// iOS also reports each new content it took on, which is the honest count of what arrived.
    private func watch() {
        guard let activity else { return }
        Task { @MainActor [weak self] in
            for await content in activity.contentUpdates {
                guard let self, self.activity?.id == activity.id else { return }
                self.deliveredThisMinute += 1
                if let rev = content.state.rev { self.observedRevision = rev }
                if UIApplication.shared.applicationState != .active { self.deliveredBackgroundThisMinute += 1 }
            }
        }
        Task { @MainActor [weak self] in
            for await state in activity.activityStateUpdates {
                guard let self else { return }
                switch state {
                case .ended, .dismissed:
                    if self.activity?.id == activity.id {
                        self.activity = nil
                        self.status = state == .dismissed ? "Dismissed; starts again when you open the app" : "Ended by iOS; starts again when you open the app"
                        self.note("live activity \(state == .dismissed ? "dismissed" : "ended by iOS")")
                    }
                    return
                case .stale:
                    self.note("live activity shows as not updating (stale)")
                default:
                    break
                }
            }
        }
    }
}
