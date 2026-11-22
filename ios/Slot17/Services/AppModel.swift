import SwiftUI
import UIKit
import WidgetKit

/// A request sent to the Pi and what became of it, for the Pi screen.
struct ActionState: Equatable {
    var id = ""
    var text = ""
    var level: Level = .idle
    var busy = false
}

final class AppModel: ObservableObject {
    enum Tab: String, CaseIterable, Identifiable {
        case live = "Live", sightings = "Sightings", pi = "Pi", logs = "Logs", settings = "Settings"
        var id: String { rawValue }
    }

    @Published var tab: Tab = .live
    @Published private(set) var headline = HeadlineStatus(level: .idle, title: "Starting…", detail: "")
    @Published private(set) var chain: [ChainItem] = []
    @Published private(set) var meters: [MeterRow] = []
    @Published private(set) var moreChannels = 0
    /// The channels beyond the three meters, most important first, at most six.
    @Published private(set) var extraMeters: [MeterRow] = []
    @Published private(set) var showBars = false
    @Published private(set) var receiverQuiet = false
    @Published private(set) var mutedSeconds = 0
    @Published private(set) var soundLine = ""
    @Published private(set) var soundOK = false
    @Published private(set) var sightings: [Sighting] = []
    @Published private(set) var alerts: [AlertRecord] = []
    @Published private(set) var logLines: [String] = []
    @Published private(set) var serverLogs: [ServerLogEntry] = []
    @Published private(set) var serverLogsNote = ""
    @Published private(set) var logUpload = ActionState()
    @Published private(set) var piLogs = ActionState()
    @Published private(set) var powerOff = ActionState()
    @Published private(set) var stopAction = ActionState()
    /// The Pushover switch on the car computer: what was last asked from this phone.
    @Published private(set) var pushoverAction = ActionState()
    @Published private(set) var doctorAction = ActionState()
    @Published private(set) var noticePermission = "Not asked yet"
    /// What this phone asked for, until the car computer reports the same (or the request fails).
    @Published private(set) var pushoverWanted: Bool?
    @Published private(set) var diagnostics: [(String, String)] = []
    @Published private(set) var hasLink = false
    @Published private(set) var linkHost = ""
    @Published private(set) var banner = ""
    @Published private(set) var style = DisplayStyle()
    @Published private(set) var configStatus = "Built-in settings"
    @Published private(set) var markSummary = MarkSummary()
    @Published private(set) var driveUpload = ActionState()
    @Published private(set) var liveTileStatus = "Off"
    @Published var settings = AlertSettings() {
        didSet {
            guard settings != oldValue else { return }
            // Remember what the operator changed by hand, so the server's file leaves it alone.
            // (Setting a property inside its own didSet does not call didSet again.)
            if !applyingRemote { settings.overrides.formUnion(AlertSettings.changedKeys(oldValue, settings)) }
            JSONStore.save(settings, "settings.json")
            applyAudioSettings()
            location.trackEvery = settings.trackEvery
            if settings.recordLocation != oldValue.recordLocation {
                if settings.recordLocation { location.start() } else { location.stop() }
            }
            if settings.summaryWindow != oldValue.summaryWindow { recomputeSummary() }
            if settings.liveActivity && !oldValue.liveActivity { liveTile?.allowImmediateStart() }
            if settings.faultNotices && !oldValue.faultNotices { notices?.ask() }
            refresh()
        }
    }
    @Published var demo = false {
        didSet {
            guard demo != oldValue else { return }
            resetLink()
            tracker.demo = demo
            demoStart = Mono.now()
            if demo { client.closeStream() } else { connect() }
            refresh()
        }
    }

    let log = EventLog()
    let audio = AudioAlerts()
    let location = LocationService()
    private var applyingRemote = false
    private var remote: RemoteConfig?
    private var remoteVersion: String?
    private var soundRetryNeeded = false
    private var lastConfigFetch = -Double.infinity
    private var carGoneSince: Double?
    private var driveUploadDoneThisStop = false
    private let client = ServerClient()
    private let tracker = LinkTracker()
    /// The Bluetooth route to the car computer, beside the server (1.4.0).
    let bluetooth = BluetoothRoute()
    /// The phone's last round trip to the server, in milliseconds.
    var phoneRoundTripMS: Double? { tracker.lastRoundTrip.map { $0 * 1000 } }
    private var engine = AlertEngine()
    private var tickTimer: DispatchSourceTimer?
    private var mutedUntil = -Double.infinity
    private var lastProbeAt = -Double.infinity
    private var probing = false
    private var lastAuthRetry = -Double.infinity
    private var lastTick = Mono.now()
    private var suspensions = 0
    private var messages = 0
    private var lastQuietTone = -Double.infinity
    private var wasLiveAt: Double?
    private var linkToneOutageDone = false
    private var asleep = false
    private var driveRetryAt = -Double.infinity
    /// Bumped whenever the link changes, so a late answer from the previous server is ignored.
    private var linkGeneration = 0
    private var lastCarLiveAt = -Double.infinity
    private var tickInterval = 0.25
    private var phoneReports: PhoneReports?
    private var linkWarningRefusalNoted = false
    private var listeningSince = Mono.now()
    private var expectingSilence = false
    private var noDataSince: Double?
    private var demoStart = Mono.now()
    private var demoSeq = 0
    private var demoBeat = 0
    private var lastResend = -Double.infinity
    private var lastOpsAt = -Double.infinity
    private var lastHistorySave = Mono.now()
    private var historyDirty = false
    private var pendingActionKinds: [String: String] = [:]
    private var liveTile: LiveTileController?
    /// Set once the tile was ended because the car was gone; it starts again only with a new drive.
    private var liveTileEndedForStop = false
    /// Becomes true the first time the person opens the app, and stays true until the app ends.
    private var listening = false
    private var lastLagNote = -Double.infinity
    /// Which software state the tile last announced, and since when.
    private var softwareKey = ""
    private var interference = InterferenceMeter()
    /// Whole-band interference in about the last minute, as a percent of the detector's windows.
    var interferencePercent: Double? { interference.percent }
    private var softwareSince = -Double.infinity
    private var notices: PhoneNotices?
    private var faultNotices = FaultNotices()

    init() {
        settings = JSONStore.load("settings.json", as: AlertSettings.self) ?? AlertSettings()
        sightings = JSONStore.load("sightings.json", as: [Sighting].self) ?? []
        alerts = JSONStore.load("alerts.json", as: [AlertRecord].self) ?? []
        audio.log = { [weak self] in self?.note($0) }
        audio.onResult = { [weak self] in self?.soundResult($0) }
        audio.onStateChange = { [weak self] in self?.refresh() }
        client.onMessage = { [weak self] in self?.received($0) }
        bluetooth.sendOp = { [weak self] kind, fields, apply in self?.sendOp(kind: kind, fields: fields, update: apply) }
        bluetooth.carState = { [weak self] in self?.tracker.frame?.message.state }
        bluetooth.deliver = { [weak self] matches, at in self?.bluetoothMatches(matches, receivedAt: at) }
        bluetooth.changed = { [weak self] in self?.refresh() }
        bluetooth.note = { [weak self] in self?.note($0) }
        client.onStreamEvent = { [weak self] in self?.note($0) }
        client.onUnauthorised = { [weak self] in self?.unauthorised() }
        applyAudioSettings()
        location.log = { [weak self] in self?.note($0) }
        location.onChange = { [weak self] in self?.refresh() }
        location.trackEvery = settings.trackEvery
        let arguments = ProcessInfo.processInfo.arguments
        UIDevice.current.isBatteryMonitoringEnabled = true
        note("app started, version \(Self.version); phone temperature \(Self.thermalText); battery \(Self.batteryText)")
        note(SharedMarks.group.map { "widget sharing on (App Group \($0))" }
             ?? "widget sharing off: this signing kept no App Group, so the widget shows only its buttons")
        for name in [UIDevice.batteryLevelDidChangeNotification, UIDevice.batteryStateDidChangeNotification] {
            NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                self?.note("battery \(Self.batteryText); phone temperature \(Self.thermalText)")
            }
        }
        phoneReports = PhoneReports(note: { [weak self] in self?.note($0) })
        NotificationCenter.default.addObserver(forName: ProcessInfo.thermalStateDidChangeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.note("phone temperature is now \(Self.thermalText)")
        }
        #if DEBUG
        if arguments.contains("--fresh") { freshForTests() }
        if let at = arguments.firstIndex(of: "--link"), at + 1 < arguments.count { _ = Keychain.save(arguments[at + 1]) }
        #endif
        loadLink()
        loadCachedConfig()
        #if DEBUG
        if arguments.contains("--demo") { demo = true; tracker.demo = true }
        if let at = arguments.firstIndex(of: "--tab"), at + 1 < arguments.count,
           let wanted = Tab(rawValue: arguments[at + 1]) { tab = wanted }
        #endif
        // Not started here. iOS sometimes starts this app in the background by itself, also after it
        // was swiped away (seen in the log on 8 November); only opening the app starts sound and link.
        recomputeSummary()
        #if DEBUG
        if arguments.contains("--seed-pending-mark") {
            MarkInbox.standard.add(PendingMark(label: .police, seenAt: Date().addingTimeInterval(-60), place: nil))
        }
        #endif
        Self.current = self
        takePendingMarks()
        liveTile = LiveTileController(note: { [weak self] in self?.note($0) })
        notices = PhoneNotices(log: { [weak self] in self?.note($0) })
        notices?.onChange = { [weak self] in
            guard let self, let text = self.notices?.permission, text != self.noticePermission else { return }
            self.noticePermission = text
        }
        refresh()
        closeIfNeverOpened()
        #if DEBUG
        if let at = arguments.firstIndex(of: "--force-sleep-after"), at + 1 < arguments.count, let seconds = Double(arguments[at + 1]) {
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in self?.goToSleep("test") }
        }
        if let at = arguments.firstIndex(of: "--simulate-close-after"), at + 1 < arguments.count, let seconds = Double(arguments[at + 1]) {
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { OrientationControl().applicationWillTerminate(UIApplication.shared) }
        }
        #endif
    }

    /// Opened by the person: everything that listens and makes sound starts now.
    private func startListening() {
        listening = true
        listeningSince = Mono.now()
        // The time before the open was not a suspension or a lag.
        lastTick = Mono.now()
        audio.arm()
        if settings.recordLocation { location.start() }
        if !demo { connect() }
        bluetooth.start()
        startTick()
    }

    /// Started by iOS with nobody opening the app: stay silent, and take away a tile left behind by an
    /// earlier run, since nothing is listening. A normal open arrives well within this wait.
    private func closeIfNeverOpened() {
        let task = UIApplication.shared.beginBackgroundTask(expirationHandler: nil)
        let release = { UIApplication.shared.endBackgroundTask(task) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            guard let self, !self.listening else { return release() }
            self.note("started by iOS in the background: staying silent until the app is opened")
            guard let tile = self.liveTile, tile.isRunning else { return release() }
            tile.end(final: self.endedTileState(Mono.now()), reason: "the app was not opened", immediately: true, finished: release)
        }
    }

    /// iOS is ending the app, for example because it was swiped away. Not every ending announces itself.
    func appIsClosing() {
        note("the app is being closed")
        log.flush()
        saveHistory()
        location.saveTrack()
        JSONStore.flush()
        liveTile?.endAndWait(final: endedTileState(Mono.now()), reason: "the app was closed")
    }

    /// For a widget button, which runs outside any screen.
    static weak var current: AppModel?

    static var batteryText: String {
        let level = UIDevice.current.batteryLevel
        let percent = level < 0 ? "unknown" : "\(Int((level * 100).rounded()))%"
        switch UIDevice.current.batteryState {
        case .charging: return percent + ", charging"
        case .full: return percent + ", full"
        case .unplugged: return percent + ", not charging"
        default: return percent
        }
    }

    /// iOS's own measure; it slows the phone down at serious and critical.
    static var thermalText: String {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: return "normal"
        case .fair: return "fair"
        case .serious: return "serious"
        case .critical: return "critical"
        @unknown default: return "unknown"
        }
    }

    static var version: String {
        let info = Bundle.main.infoDictionary
        return "\(info?["CFBundleShortVersionString"] ?? "?") (\(info?["CFBundleVersion"] ?? "?"))"
    }

    #if DEBUG
    private func freshForTests() {
        Keychain.delete()
        log.clear()
        sightings = []
        alerts = []
        settings = AlertSettings()
        location.clearTrack()
        try? FileManager.default.removeItem(at: Self.configCache)
        UserDefaults.standard.removeObject(forKey: "lastDriveUpload")
    }
    #endif

    // MARK: Connection

    private func loadLink() {
        let link = Keychain.load().flatMap(ServerLink.parse)
        client.configure(link)
        hasLink = link != nil
        if let link, let host = link.base.host {
            linkHost = link.base.port.map { "\(host):\($0)" } ?? host
        } else {
            linkHost = ""
        }
    }

    /// Returns a reason if the text is not a private link.
    func setLink(_ text: String) -> String? {
        guard ServerLink.parse(text) != nil else { return "That is not a private link. It looks like http://address:port/?key=..." }
        guard Keychain.save(text.trimmingCharacters(in: .whitespacesAndNewlines)) else { return "The phone could not store it." }
        let previousHost = linkHost
        loadLink()
        if linkHost != previousHost { forgetServerSettings() }
        resetLink()
        connect()
        note("private link changed; server \(linkHost)")
        refresh()
        return nil
    }

    func removeLink() {
        Keychain.delete()
        client.closeStream()
        loadLink()
        resetLink()
        refresh()
    }

    /// Another server's rules, words and sounds must not stay in use on this one.
    private func forgetServerSettings() {
        try? FileManager.default.removeItem(at: Self.configCache)
        remote = nil
        remoteVersion = nil
        soundRetryNeeded = false
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "lastDriveUpload")
        for index in sightings.indices where sightings[index].needsSending {
            sightings[index].phase = .failed
            sightings[index].detail = "Not sent: the private link changed"
        }
        JSONStore.save(sightings, "sightings.json")
        applyingRemote = true
        var updated = settings
        updated.restoreBuiltInRules()
        settings = updated
        applyingRemote = false
        banner = ""
        style = DisplayStyle()
        for kind in [SoundKind.urgent, .chime, .call, .warning, .linkLost] {
            audio.setSound(kind, samples: SoundSynth.render(kind.builtIn))
        }
        configStatus = "Built-in settings"
    }

    private func resetLink() {
        linkGeneration += 1
        probing = false
        tracker.reset()
        engine = AlertEngine()
        wasLiveAt = nil
        linkToneOutageDone = false
        expectingSilence = false
        lastProbeAt = -Double.infinity
    }

    private func connect() {
        guard hasLink, !demo else { return }
        client.openStream()
        probe()
        fetchConfig()
    }

    // MARK: Settings from the server

    private static var configCache: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("remote-config.json")
    }

    /// The last good settings stay in use when the server cannot be reached.
    private func loadCachedConfig() {
        guard let data = try? Data(contentsOf: Self.configCache), let decoded = RemoteConfig.decode(data),
              decoded.config.problems.isEmpty else { return }
        applyRemote(version: decoded.version, config: decoded.config, source: "saved copy")
    }

    func fetchConfig() {
        guard hasLink, !demo else { return }
        lastConfigFetch = Mono.now()
        let generation = linkGeneration
        client.fetchConfig { [weak self] result in
            guard let self, generation == self.linkGeneration else { return }
            switch result {
            case .success(let data):
                guard let decoded = RemoteConfig.decode(data) else {
                    self.configStatus = "Server settings unreadable; keeping the current ones"
                    self.note("PROBLEM server settings could not be read")
                    return
                }
                let problems = decoded.config.problems
                guard problems.isEmpty else {
                    self.configStatus = "Server settings refused: \(problems.prefix(2).joined(separator: "; "))"
                    self.note("PROBLEM server settings refused: \(problems.joined(separator: "; "))")
                    return
                }
                guard decoded.version != self.remoteVersion || self.soundRetryNeeded else { return }
                try? data.write(to: Self.configCache, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
                self.applyRemote(version: decoded.version, config: decoded.config, source: "server")
            case .failure(let error):
                if ServerClient.status(of: error) == 404, self.remote == nil {
                    self.configStatus = "Built-in settings (none on the server)"
                }
            }
        }
    }

    private func applyRemote(version: String, config: RemoteConfig, source: String) {
        remote = config
        remoteVersion = version
        applyingRemote = true
        var updated = settings
        updated.apply(config)
        settings = updated
        applyingRemote = false
        banner = config.message ?? ""
        soundRetryNeeded = false
        var newStyle = DisplayStyle()
        newStyle.apply(config.display)
        if newStyle != style { style = newStyle }
        for kind in [SoundKind.urgent, .chime, .call, .warning, .linkLost] {
            let name = kind == .linkLost ? "link_lost" : kind.rawValue
            let spec = config.sounds?[name] ?? kind.builtIn
            if let wav = spec.wav {
                loadServerSound(kind, wav)
            } else {
                audio.setSound(kind, samples: SoundSynth.render(spec))
            }
        }
        configStatus = source == "server" ? "Server, version \(version)" : "Saved copy of server version \(version)"
        note("settings \(version) applied from the \(source)\(settings.overrides.isEmpty ? "" : "; kept your own: \(settings.overrides.sorted().joined(separator: ", "))")")
    }

    private func loadServerSound(_ kind: SoundKind, _ name: String) {
        let generation = linkGeneration
        client.fetchSound(name) { [weak self] result in
            guard let self, generation == self.linkGeneration else { return }
            if case .success(let data) = result, let samples = self.audio.samples(fromWAV: data) {
                self.audio.setSound(kind, samples: samples)
                self.note("sound \(kind.rawValue) now plays \(name) from the server")
            } else {
                self.soundRetryNeeded = true
                self.note("PROBLEM sound \(name) could not be used; \(kind.rawValue) keeps its current sound for now")
            }
        }
    }

    /// Drops the operator's own changes to rules, so the server's (or built-in) values apply again.
    func useServerSettings() {
        applyingRemote = true
        var reset = settings
        let defaults = AlertSettings()
        reset.overrides = []
        reset.red = defaults.red
        reset.yellow = defaults.yellow
        reset.quietRed = defaults.quietRed
        reset.quietGap = defaults.quietGap
        if let remote { reset.apply(remote) }
        settings = reset
        applyingRemote = false
        note("own rule changes cleared; using \(remote == nil ? "built-in" : "server") settings")
    }

    private func probe() {
        guard !probing, !demo, hasLink else { return }
        probing = true
        lastProbeAt = Mono.now()
        let generation = linkGeneration
        client.probe { [weak self] result, begin, end in
            guard let self, generation == self.linkGeneration else { return }
            self.probing = false
            switch result {
            case .success(let message):
                self.handleAcceptance(self.tracker.applyProbe(message, begin: begin, end: end), message)
                self.refresh()
            case .failure(let error):
                if ServerClient.status(of: error) == 401 { self.unauthorised() }
            }
        }
    }

    private func unauthorised() {
        guard !tracker.authFailed else { return }
        tracker.authFailed = true
        engine.clearWaiting()
        client.closeStream()
        note("PROBLEM the server refused the private link")
        refresh()
    }

    private func received(_ message: ServerMessage) {
        guard !demo else { return }
        handleAcceptance(tracker.accept(message, now: Mono.now()), message)
        if message.release != nil { announceUpdates() }
        if !tracker.clockReady(Mono.now()), Mono.now() - lastProbeAt >= 2 { probe() }
        refresh()
        if listening { updateLiveTile(Mono.now()) }
    }

    private func handleAcceptance(_ result: LinkTracker.Acceptance, _ message: ServerMessage) {
        if result == .refused { return }
        messages += 1
        if result == .freshAndNewScope { engine.resetContext() }
        if result == .fresh || result == .freshAndNewScope {
            if let detector = message.state?.detector, let windows = detector.windows, let wide = detector.wholeBandWindows {
                interference.add(at: Mono.now(), windows: windows, wide: wide)
            }
            serverMatches(message.state?.matches ?? [])
            engine.ingest(message.state?.matches ?? [], frame: tracker.frame,
                          channels: message.state?.channels ?? [], scope: tracker.scope)
        }
        flushMatches()
        applyOps(message.ops ?? [], command: message.command)
    }

    /// Matches from the server: counted for the route figures, and an alert first heard over Bluetooth takes the
    /// server's id, so the drive report can find it.
    private func serverMatches(_ matches: [MatchRow]) {
        let now = Mono.now()
        for match in matches {
            let key = MatchKey.of(match)
            bluetooth.serverMatch(key, at: now)
            if let index = alerts.firstIndex(where: { $0.id == "bt:" + key }) {
                alerts[index].id = match.id
                historyDirty = true
            }
        }
    }

    /// Matches over Bluetooth: their age counts from when they arrived, so no server clock is needed.
    private func bluetoothMatches(_ matches: [MatchRow], receivedAt: Double) {
        engine.ingest(matches, frame: nil, channels: bluetooth.feed.channels, scope: tracker.scope, receivedAt: receivedAt)
        flushMatches()
        refresh()
        if listening { updateLiveTile(Mono.now()) }
    }

    private func flushMatches() {
        let now = Mono.now()
        let context = AlertEngine.Context(
            now: now, live: tracker.live(now) || bluetooth.feed.live(now), mayHold: tracker.mayHoldMatches(now), scope: tracker.scope,
            settings: settings, armed: audio.armed, audioProblem: audio.problem, mutedUntil: mutedUntil,
            age: { [tracker] waiting in
                if let receivedAt = waiting.receivedAt { return waiting.match.age + (now - receivedAt) }
                guard let frame = waiting.frame else { return .infinity }
                return waiting.match.age + tracker.elapsed(frame, now)
            },
            play: { [audio] in audio.play($0) })
        for decision in engine.flush(context) { record(decision) }
    }

    private func record(_ d: AlertDecision) {
        var sound: SoundKind?
        var reason = ""
        switch d.outcome {
        case .played(let kind):
            sound = kind
            note(String(format: "%@ for L%d, %@, %.1f s old", kind.rawValue, d.channel, Self.db(d.strength), d.age))
        case .silent(let why):
            reason = why
            note(String(format: "silent for L%d, %@: %@", d.channel, Self.db(d.strength), why))
        }
        alerts.insert(AlertRecord(id: d.id, at: Date(), channel: d.channel, strength: d.strength, age: d.age,
                                  sound: sound, reason: reason, place: settings.recordLocation ? location.place : nil,
                                  rule: d.rule), at: 0)
        if alerts.count > 1000 { alerts.removeLast(alerts.count - 1000) }
        historyDirty = true
        if sound != nil { recomputeSummary() }
    }

    private func recomputeSummary() {
        markSummary = MarkSummary.make(alerts: alerts, sightings: sightings, window: settings.summaryWindow)
    }

    private static func db(_ value: Double?) -> String {
        value.map { String(format: "%.1f dB", $0) } ?? "no strength"
    }

    private func soundResult(_ result: AudioAlerts.Result) {
        switch result {
        case .played(let kind, let peak):
            note(String(format: "sound played: %@, peak %.1f dBFS, output %@", kind.rawValue, peak, audio.routeName))
        case .failed(let kind, let why):
            note("PROBLEM sound \(kind.rawValue) \(why)")
            if let index = alerts.firstIndex(where: { $0.sound == kind && Date().timeIntervalSince($0.at) < 15 }) {
                alerts[index].sound = nil
                alerts[index].reason = "the sound failed: \(why)"
                historyDirty = true
                recomputeSummary()
            }
        }
    }

    // MARK: The regular tick

    private func startTick() {
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + tickInterval, repeating: tickInterval, leeway: .milliseconds(50))
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume()
        tickTimer = timer
    }

    /// 4 a second on screen, once a second behind it. Sounds and the tile do not wait for this: both act
    /// the moment a message arrives (`received`).
    private func setTickInterval(_ seconds: Double) {
        guard seconds != tickInterval else { return }
        tickInterval = seconds
        tickTimer?.schedule(deadline: .now() + seconds, repeating: seconds, leeway: .milliseconds(seconds >= 1 ? 200 : 50))
    }

    private func tick() {
        let now = Mono.now()
        guard !asleep else {
            lastTick = now
            return
        }
        // A tick much later than due, that is not a suspension, means the app really lagged.
        let late = now - lastTick
        if late > tickInterval + 1, late <= 5, now - lastLagNote >= 60 {
            lastLagNote = now
            note(String(format: "app lagged: %.1f s between ticks; phone temperature %@", late, Self.thermalText))
        }
        if now - lastTick > 5 {
            suspensions += 1
            note(String(format: "PROBLEM iPhone stopped this app for %.0f s", now - lastTick))
        }
        lastTick = now
        if demo {
            let message = demoMessage(now)
            handleAcceptance(tracker.accept(message, now: now), message)
        }
        watchdog(now)
        flushMatches()
        checkEquipmentTones(now)
        checkAutoSleep(now)
        checkDriveEnded(now)
        bluetooth.settle(now)
        updateLocationPower(now)
        resendPending(now)
        checkFaultNotices(now)
        if hasLink, !demo, now - lastConfigFetch >= 300 { fetchConfig() }
        if historyDirty && now - lastHistorySave > 20 { saveHistory() }
        refresh()
        updateLiveTile(now)
    }

    // MARK: The Live Activity (Lock Screen, Dynamic Island, CarPlay Dashboard)

    private func liveTileState(_ now: Double) -> LiveTileState {
        // The same test as the meters: fresh car data, a usable clock, and the key accepted.
        // A server that answers while the Pi's data is old is not "listening".
        let carFresh = tracker.frame != nil && tracker.clockReady(now) && tracker.carAge(now) <= LinkTracker.carStale
            && tracker.sinceRx(now) <= LinkTracker.linkLost && !tracker.authFailed
        var heardAt: Date?
        var burstID: String?
        if let top = meters.first, let frame = tracker.frame,
           let channel = frame.message.state?.channels.first(where: { $0.ch == top.ch }) {
            heardAt = Date().addingTimeInterval(-(channel.age + tracker.elapsed(frame, now)))
            burstID = channel.burstID
        }
        return LiveTileBuilder.make(.init(
            linkLost: !carFresh && !demo, detectorRunning: tracker.detectorActive || demo,
            receiverQuiet: receiverQuiet, top: meters.first,
            lastAlert: alerts.first { $0.sound == .urgent || $0.sound == .chime },
            lastMark: sightings.first { !$0.isRecordingRequest },
            settings: settings, style: style, demo: demo, now: Date(),
            topHeardAt: heardAt, topBurstID: burstID, previous: liveTile?.lastSent,
            software: softwareNote(now),
            othersActive: max(0, meters.count + moreChannels - 1),
            carAge: tracker.frame != nil ? tracker.carAge(now) : nil))
    }

    /// The tile's word about the car computer's software, for a few minutes after its state changes.
    private func softwareNote(_ now: Double) -> LiveTileState.Software? {
        guard !demo, let update = tracker.frame?.message.state?.update, let made = SoftwareNote.make(update) else { return nil }
        let key = "\(update.state) \(update.seq ?? 0)"
        if key != softwareKey {
            softwareKey = key
            softwareSince = now
            let detail = update.detail ?? ""
            note("car software: \(made.software.text)\(detail.isEmpty ? "" : " (\(detail))")")
        }
        return now - softwareSince < made.shownFor ? made.software : nil
    }

    /// What the tile shows as it goes: no signal values, only that it ended.
    private func endedTileState(_ now: Double) -> LiveTileState {
        var last = liveTileState(now)
        last.phase = .ended
        last.channel = nil; last.strength = nil; last.level = 0; last.tone = .none
        last.frequent = false; last.heardAt = nil
        return last
    }

    private func updateLiveTile(_ now: Double) {
        guard let liveTile else { return }
        defer { if liveTileStatus != liveTile.status { liveTileStatus = liveTile.status } }
        let wanted = settings.liveActivity && (hasLink || demo) && audio.armed
        guard wanted else {
            if liveTile.isRunning {
                liveTile.end(final: endedTileState(now),
                             reason: settings.liveActivity ? "sound asleep or no link" : "switched off in Settings",
                             immediately: !settings.liveActivity)
            }
            return
        }
        // The drive is over once the car has been gone for 10 minutes: end the tile with a last word.
        if !demo, let gone = carGoneSince, now - gone >= 600 {
            if liveTile.isRunning && !liveTileEndedForStop {
                liveTileEndedForStop = true
                liveTile.end(final: endedTileState(now), reason: "car gone for 10 minutes", immediately: false)
            }
            return
        }
        liveTile.minInterval = style.tileGap
        let state = liveTileState(now)
        if liveTile.isRunning {
            liveTile.update(state)
        } else if UIApplication.shared.applicationState == .active {
            liveTileEndedForStop = false
            liveTile.start(state)
        }
    }

    private func watchdog(_ now: Double) {
        guard hasLink, !demo else { return }
        if tracker.authFailed {
            if now - lastAuthRetry > 15 { lastAuthRetry = now; retryRefusedLink() }
            return
        }
        let silent = tracker.frame != nil ? now - tracker.rxAt : now - client.openedAt
        if silent > LinkTracker.reopenAfter && now - client.openedAt > LinkTracker.reopenAfter {
            client.openStream()
        }
        if now - lastProbeAt >= LinkTracker.clockRefresh { probe() }
    }

    /// While the key is refused, ask again in case it starts working, without any stream.
    private func retryRefusedLink() {
        let generation = linkGeneration
        client.probe { [weak self] result, begin, end in
            guard let self, !self.asleep, generation == self.linkGeneration, case .success(let message) = result else { return }
            self.tracker.authFailed = false
            self.handleAcceptance(self.tracker.applyProbe(message, begin: begin, end: end), message)
            self.client.openStream()
            self.note("the server accepts the private link again")
        }
    }

    private func checkEquipmentTones(_ now: Double) {
        guard !demo, audio.armed, settings.sound, now >= mutedUntil else { return }
        let live = tracker.live(now)
        if live {
            wasLiveAt = now
            linkToneOutageDone = false
            expectingSilence = false
        }
        if tracker.detectorState == "stopped" { expectingSilence = true }
        if settings.linkLostTone, let last = wasLiveAt, !live, !expectingSilence, !linkToneOutageDone,
           now - last >= settings.linkLostAfter {
            playLinkWarning(String(format: "link lost for %.0f s: playing the warning, silence no longer means anything", settings.linkLostAfter))
        }
        // Opened in the car, and nothing has ever arrived: as silent as a quiet road, so it must be said.
        // Not away from the car, where an unpowered Pi is normal.
        if settings.linkLostTone, wasLiveAt == nil, !live, !expectingSilence, !linkToneOutageDone, routeIsCarForWarning,
           now - listeningSince >= max(settings.linkLostAfter, 60) {
            playLinkWarning("never connected since the app was opened: playing the warning, silence means nothing")
        }
        if StatusBuilder.receiverQuiet(tracker, settings: settings, now: now), now - lastQuietTone >= settings.receiverToneRepeat {
            lastQuietTone = now
            note("receiver hears nothing: playing the warning")
            audio.play(.warning)
        }
    }

    /// The same faults Pushover sends, as notifications from this app, when switched on in Settings.
    private func checkFaultNotices(_ now: Double) {
        guard settings.faultNotices, !demo, let notices else { return }
        let detector = piState?.detector
        let input = FaultNotices.Input(detectorState: detector?.state, reasons: detector?.reasons ?? [],
                                       receiverQuiet: receiverQuiet, expectingSilence: expectingSilence)
        if let notice = faultNotices.step(input, now: now) { notices.post(notice) }
    }

    private var routeIsCarForWarning: Bool {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--pretend-car-audio") { return true }
        #endif
        return audio.routeIsCar
    }

    /// Counts as done only if the sound really started; otherwise the next tick tries again.
    private func playLinkWarning(_ text: String) {
        if let refused = audio.play(.linkLost) {
            if !linkWarningRefusalNoted {
                linkWarningRefusalNoted = true
                note("PROBLEM the link warning could not play: \(refused)")
            }
        } else {
            linkToneOutageDone = true
            linkWarningRefusalNoted = false
            note(text)
        }
    }

    /// Without this the app would keep the audio loop running all day after parking. Once
    /// asleep it needs opening again, because iOS will not start audio from the background.
    private func checkAutoSleep(_ now: Double) {
        guard settings.autoSleepMinutes > 0, !demo else { return }
        let carLive = tracker.frame != nil && tracker.carAge(now) <= LinkTracker.carStale && tracker.detectorActive
            || bluetooth.feed.live(now)
        if carLive || UIApplication.shared.applicationState == .active {
            noDataSince = nil
            if !audio.armed && UIApplication.shared.applicationState == .active { audio.arm() }
            return
        }
        guard audio.armed else { return }
        let since = noDataSince ?? now
        noDataSince = since
        if now - since >= Double(settings.autoSleepMinutes) * 60 {
            goToSleep("no car data for \(settings.autoSleepMinutes) min with the app closed")
        }
    }

    /// Everything that uses power stops, so iOS can suspend the app. Opening it starts it all again.
    private func goToSleep(_ why: String) {
        asleep = true
        note("going to sleep: \(why)")
        audio.disarm(why)
        location.stop()
        client.closeStream()
        bluetooth.stop()
        if liveTile?.isRunning == true {
            liveTile?.end(final: endedTileState(Mono.now()), reason: "asleep", immediately: false)
        }
        refresh()
    }

    /// GPS is for records only, so with no car data it drops to a coarse, low-power setting, and returns
    /// when data comes back. Not stopped: iOS would not let it start again from the background.
    private func updateLocationPower(_ now: Double) {
        let carLive = tracker.frame != nil && tracker.carAge(now) <= LinkTracker.carStale || bluetooth.feed.live(now)
        if carLive { lastCarLiveAt = now }
        let open = UIApplication.shared.applicationState == .active
        location.setLowPower(!open && !carLive && now - max(lastCarLiveAt, listeningSince) >= 120)
    }

    /// Once the car has been gone for 10 minutes, the drive is probably over: send its data once,
    /// so sightings and alerts can be analysed with their places without anyone remembering to.
    private func checkDriveEnded(_ now: Double) {
        guard hasLink, !demo else { return }
        let carLive = tracker.frame != nil && tracker.carAge(now) <= LinkTracker.carStale || bluetooth.feed.live(now)
        if carLive {
            carGoneSince = nil
            driveUploadDoneThisStop = false
            return
        }
        guard tracker.frame != nil, !driveUploadDoneThisStop else { return }
        let since = carGoneSince ?? now
        carGoneSince = since
        // Soon: on 8 November iOS had suspended the app within minutes of parking, so a 10-minute
        // wait never arrived. What this misses is sent the next time the app opens.
        if now - since >= 180, now >= driveRetryAt, !driveUpload.busy, hasNewDriveData {
            driveUploadDoneThisStop = true
            sendDriveRecords()
        }
    }

    private var lastDriveUpload: Date {
        Date(timeIntervalSince1970: UserDefaults.standard.double(forKey: "lastDriveUpload"))
    }

    /// The drive's data and the app's own log, so a drive can be reviewed without asking for them.
    private func sendDriveRecords() {
        uploadDriveData(automatic: true)
        uploadAppLog()
    }

    /// Records of an earlier drive that never got sent, because iOS suspended the app first.
    private func sendLeftoverDriveRecords() {
        guard hasLink, !demo, hasNewDriveData, !driveUpload.busy else { return }
        let newest = [alerts.first?.at, sightings.first?.seenAt].compactMap { $0 }.max() ?? .distantPast
        if Date().timeIntervalSince(newest) >= 600 {
            note("sending the records of the last drive, which were not sent before the app was suspended")
            sendDriveRecords()
        }
    }

    private var hasNewDriveData: Bool {
        alerts.contains { $0.at > lastDriveUpload } || sightings.contains { $0.seenAt > lastDriveUpload }
    }

    /// Alerts, sightings and the location track since the last upload, as one JSON file on the server.
    func uploadDriveData(automatic: Bool = false) {
        let from = lastDriveUpload
        var until = Date()
        let everything = until
        struct DriveData: Encodable {
            let kind = "slot17-drive-data"
            let app: String
            let from: Date
            let until: Date
            let settings: AlertSettings
            let alerts: [AlertRecord]
            let sightings: [Sighting]
            let track: [LocationService.TrackPoint]
        }
        let waitingAlerts = alerts.filter { $0.at > from }.sorted { $0.at < $1.at }
        let waitingTrack = location.track.filter { $0.at > from }.sorted { $0.at < $1.at }
        let recentSightings = sightings.filter { $0.seenAt > from.addingTimeInterval(-3600) }
        var data = DriveData(app: Self.version, from: from, until: until, settings: settings, alerts: waitingAlerts,
                             sightings: recentSightings, track: waitingTrack)
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        guard var body = try? encoder.encode(data) else { return }
        // The server takes at most 1 MB. Too much goes in parts, oldest first, up to a point in time
        // that only moves when a part is stored, so no record is ever skipped.
        var limit = 800
        while body.count > 950_000, limit >= 25 {
            let part = Array(waitingAlerts.prefix(limit)), track = Array(waitingTrack.prefix(limit * 5))
            let ends = [part.count < waitingAlerts.count ? part.last?.at : nil, track.count < waitingTrack.count ? track.last?.at : nil].compactMap { $0 }
            let oldestFirst = recentSightings.sorted { $0.seenAt < $1.seenAt }
            let marks = Array(oldestFirst.prefix(limit))
            let markEnd = marks.count < oldestFirst.count ? marks.last?.seenAt : nil
            until = (ends + [markEnd].compactMap { $0 }).min() ?? everything
            data = DriveData(app: Self.version, from: from, until: until, settings: settings, alerts: part.filter { $0.at <= until },
                             sightings: marks.filter { $0.seenAt <= until }, track: track.filter { $0.at <= until })
            body = (try? encoder.encode(data)) ?? body
            limit /= 2
        }
        let partial = until < everything
        let sentUntil = until, sent = data
        driveUpload = .init(text: "Sending…", busy: true)
        client.uploadLog(body) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let reply):
                let name = (try? JSONSerialization.jsonObject(with: reply) as? [String: Any])?["name"] as? String ?? "on the server"
                UserDefaults.standard.set(sentUntil.timeIntervalSince1970, forKey: "lastDriveUpload")
                if partial { self.driveUploadDoneThisStop = false }   // the next part follows by itself
                self.driveUpload = .init(text: "\(automatic ? "Sent automatically" : "Sent") as \(name)\(partial ? ", more to come" : "")", level: .ok)
                self.note("drive data sent as \(name) (\(sent.alerts.count) alerts, \(sent.sightings.count) sightings, \(sent.track.count) places)\(partial ? ", more to come" : "")")
                self.refreshServerLogs()
            case .failure(let error):
                self.driveUpload = .init(text: "Could not send: \(error.localizedDescription). It stays on this phone and will be tried again.", level: .bad)
                if automatic {
                    self.driveUploadDoneThisStop = false
                    self.driveRetryAt = Mono.now() + 120
                }
            }
        }
    }

    func becameActive() {
        #if DEBUG
        // Stands in for a background launch, which a test cannot cause: the app is never "opened".
        if ProcessInfo.processInfo.arguments.contains("--simulate-background-launch") { return }
        #endif
        note("app opened; output \(audio.routeName)")
        notices?.readPermission()       // it may have been changed in iPhone Settings meanwhile
        asleep = false
        noDataSince = nil
        setTickInterval(0.25)
        liveTile?.allowImmediateStart()
        if !listening {
            startListening()
            sendLeftoverDriveRecords()
            refresh()
            return
        }
        sendLeftoverDriveRecords()
        if !audio.armed { audio.arm() }
        if settings.recordLocation { location.start() }
        if !demo, hasLink, !tracker.authFailed, Mono.now() - tracker.rxAt > LinkTracker.linkLost { connect() }
        refresh()
    }

    func movedToBackground() {
        setTickInterval(1)
        saveHistory()
        location.saveTrack()
        JSONStore.flush()
        note("app moved to the background; output \(audio.routeName)")
        log.flush()
    }

    private func saveHistory() {
        historyDirty = false
        lastHistorySave = Mono.now()
        JSONStore.save(alerts, "alerts.json")
    }

    // MARK: Refresh what the screen shows

    private func refresh() {
        let now = Mono.now()
        let newHeadline = StatusBuilder.headline(tracker, now: now, lastDelay: engine.lastDelay, hasCredentials: hasLink || demo)
        if newHeadline != headline { headline = newHeadline }
        let newChain = StatusBuilder.chain(tracker, now: now, hasCredentials: hasLink || demo,
                                           quietWindows: StatusBuilder.quietLimit(tracker.frame?.message.state?.detector, settings))
        if newChain != chain { chain = newChain }

        let state = tracker.frame?.message.state
        let carFresh = tracker.frame != nil && tracker.clockReady(now) && tracker.carAge(now) <= LinkTracker.carStale
            && tracker.sinceRx(now) <= LinkTracker.linkLost
        // Every assignment to a @Published value redraws every screen watching the model, even when
        // nothing changed; this runs four times a second, so only real changes are assigned.
        let overBluetooth = !carFresh && bluetooth.feed.live(now)
        if overBluetooth {
            let line = HeadlineStatus(level: .ok, title: "Live over Bluetooth",
                                      detail: "No link to the server: alerts come straight from the car computer")
            if line != headline { headline = line }
        }
        let bars = carFresh && tracker.detectorActive || overBluetooth
        if bars != showBars { showBars = bars }
        var rows: [MeterRow] = []
        if overBluetooth, let state = bluetooth.feed.piState(base: nil, now: now) {
            rows = Meters.pick(state.channels.map {
                let age = ($0.age * 2).rounded(.down) / 2
                return MeterRow(ch: $0.ch, freqMHz: $0.freqMHz, strength: $0.strength, age: age,
                                hits60: $0.hits60, approxTime: $0.approxTime ?? false,
                                frequent: $0.activity == "active" && age < 15,
                                rising: ($0.rising ?? false) && age < 20, call: $0.call ?? false)
            })
        } else if bars, let frame = tracker.frame, let state {
            let elapsed = tracker.elapsed(frame, now)
            rows = Meters.pick(state.channels.map {
                // Ages in half seconds: the screen shows whole seconds, and finer steps only redraw.
                let age = (($0.age + elapsed) * 2).rounded(.down) / 2
                return MeterRow(ch: $0.ch, freqMHz: $0.freqMHz, strength: $0.strength, age: age,
                                hits60: $0.hits60, approxTime: $0.approxTime ?? false,
                                frequent: $0.activity == "active" && age < 15,
                                rising: ($0.rising ?? false) && age < 20, call: $0.call ?? false)
            })
        }
        let more = max(0, rows.count - 3)
        if more != moreChannels { moreChannels = more }
        let extra = Array(rows.dropFirst(3).prefix(6))
        if extra != extraMeters { extraMeters = extra }
        let shown = Array(rows.prefix(3))
        if shown != meters { meters = shown }
        let quiet = StatusBuilder.receiverQuiet(tracker, settings: settings, now: now)
        if quiet != receiverQuiet { receiverQuiet = quiet }
        settlePushoverWanted()
        bluetooth.settleWanted()
        let muted = now < mutedUntil ? Int((mutedUntil - now).rounded(.up)) : 0
        if muted != mutedSeconds { mutedSeconds = muted }

        var problem: String?
        if !settings.sound {
            problem = "sound is switched off"
        } else {
            problem = audio.problem
        }
        if soundOK != (problem == nil) { soundOK = problem == nil }
        let line: String
        if let problem {
            line = "No sound: \(problem)"
        } else if audio.recovering {
            line = "Sound is restarting after a problem. It may be silent for a moment."
        } else if !engine.lastSilenceReason.isEmpty {
            line = "Sound ready. Last match was silent: \(engine.lastSilenceReason)"
        } else {
            line = "Sound ready, output \(audio.routeName)"
        }
        if line != soundLine { soundLine = line }
        // Only while the Logs screen shows them: a line comes with every match, and each change here
        // would redraw every screen for a list nobody is looking at.
        if tab == .logs, log.recent.count != logLines.count || log.recent.last != logLines.last { logLines = log.recent }
        refreshDiagnostics(now, state)
    }

    private func refreshDiagnostics(_ now: Double, _ state: PiState?) {
        guard tab == .logs || tab == .pi else { return }
        let format = { (v: Double) in v.isFinite ? String(format: "%.1f s", v) : "-" }
        let rows: [(String, String)] = [
            ("App version", Self.version),
            ("Server", linkHost.ifEmpty("not set")),
            ("Connections opened", "\(client.streamOpens)"),
            ("Messages received", "\(messages)"),
            ("Last message", format(tracker.sinceRx(now))),
            ("Car data age", format(tracker.carAge(now))),
            ("Timing sample", tracker.clock.map { format(now - $0.at) } ?? "none"),
            ("Burst delay", engine.lastDelay.map(format) ?? "-"),
            ("Sound armed", audio.armed ? "yes" : "no"),
            ("Phone temperature", Self.thermalText),
            ("Battery", Self.batteryText),
            ("Audio engine", audio.engineRunning ? "running" : "stopped"),
            ("Audio output", audio.routeName),
            ("Other audio playing", audio.otherAudioPlaying ? "yes" : "no"),
            ("Sounds played", "\(audio.soundsPlayed)"),
            ("Last sound peak", audio.lastPeakDB.map { String(format: "%.1f dBFS", $0) } ?? "-"),
            ("Audio recoveries", "\(audio.recoveries)"),
            ("Times iPhone stopped the app", "\(suspensions)"),
            ("Last silence reason", engine.lastSilenceReason.ifEmpty("-")),
            ("Settings", configStatus),
            ("Own rule changes", settings.overrides.isEmpty ? "none" : settings.overrides.sorted().joined(separator: ", ")),
            ("Location", location.statusText),
            ("Places recorded", "\(location.track.count)"),
            ("Session folder (clock may be wrong)", state?.session ?? "-"),
            ("Live Activity", liveTileStatus),
        ]
        if !rows.elementsEqual(diagnostics, by: ==) { diagnostics = rows }
    }

    // MARK: Info for the Pi screen

    var piState: PiState? {
        let now = Mono.now()
        if let frame = tracker.frame, tracker.carAge(now) <= LinkTracker.carStale { return frame.message.state }
        // Without the server, the car computer's own state over Bluetooth, on top of the server's last report.
        return bluetooth.feed.live(now) ? bluetooth.feed.piState(base: tracker.frame?.message.state, now: now) : nil
    }

    /// Why this action cannot be used right now, in words, or nil if it can.
    func unavailable(_ feature: String) -> String? {
        if demo { return "Not available in demo mode" }
        guard let state = piState else { return "The Pi is not sending data" }
        if !tracker.detectorActive { return "The detector is not running, so the Pi has no link to the app" }
        if !(state.features ?? []).contains(feature) { return "The Pi needs the newer publisher for this" }
        return nil
    }

    // MARK: Sound controls

    func test(_ kind: SoundKind) {
        if !audio.armed { audio.arm() }
        note("sound test: \(kind.rawValue)")
        if let refused = audio.play(kind) { note("PROBLEM sound test refused: \(refused)") }
    }

    func toggleMute() {
        let now = Mono.now()
        mutedUntil = now < mutedUntil ? -Double.infinity : now + 30
        engine.clearWaiting()
        refresh()
    }

    private func applyAudioSettings() {
        audio.volume = Float(settings.volume)
        audio.style = settings.beepStyle
    }

    // MARK: Sightings

    func mark(_ label: Sighting.Label, note text: String = "", secondsAgo: TimeInterval = 0, recording: Bool = false) {
        let seenAt = Date().addingTimeInterval(-secondsAgo)
        if !recording, Sighting.isRepeat(label, at: seenAt, among: sightings) {
            note("sighting: \(label.rawValue) again within \(Int(Sighting.repeatWindow)) s, counted once")
            return
        }
        let sighting = Sighting(id: Sighting.newID(), label: label, note: text, seenAt: seenAt,
                                phase: .sending, detail: "", isRecordingRequest: recording,
                                place: settings.recordLocation && secondsAgo < 30 ? location.place : nil)
        sightings.insert(sighting, at: 0)
        if sightings.count > 500 { sightings.removeLast(sightings.count - 500) }
        JSONStore.save(sightings, "sightings.json")
        recomputeSummary()
        note("sighting: \(label.rawValue)\(recording ? " (recording request)" : "")\(sighting.place.map { " at " + $0.noteText } ?? "")")
        UINotificationFeedbackGenerator().notificationOccurred(.success)
        send(sighting)
        if !recording { Self.shareMark(label, at: seenAt) }
    }

    /// For the widget: the last sighting, through the App Group when this signing kept one.
    static func shareMark(_ label: Sighting.Label, at: Date) {
        guard SharedMarks.group != nil else { return }
        SharedMarks.save(.init(kind: label.rawValue, title: label.title, at: at))
        WidgetCenter.shared.reloadTimelines(ofKind: "Slot17Mark")
    }

    /// A widget button: a normal sighting, plus a short sound and the tile, because the driver is not
    /// looking at the phone and the widget itself cannot know it worked.
    func markFromWidget(_ label: Sighting.Label, tappedAt: Date) {
        mark(label, note: "widget", secondsAgo: max(0, Date().timeIntervalSince(tappedAt)))
        if let refused = audio.play(.marked) { note("sound for the widget mark not played: \(refused)") }
        // Only while listening: asleep, or started by iOS, this would otherwise end the tile.
        if listening, !asleep { updateLiveTile(Mono.now()) }
        JSONStore.flush()
    }

    /// Taps saved while the app had no model. Kept until they are safely in the sightings file.
    private func takePendingMarks() {
        let pending = MarkInbox.standard.all
        guard !pending.isEmpty else { return }
        for p in pending {
            // Its id stays with it, so ending the app between these two saves cannot add it twice.
            let id = p.id ?? Sighting.newID()
            if sightings.contains(where: { $0.id == id }) { continue }
            let sighting = Sighting(id: id, label: p.label, note: "widget, app was closed", seenAt: p.seenAt,
                                    phase: .sending, detail: "", isRecordingRequest: false,
                                    place: settings.recordLocation ? p.place : nil)
            sightings.insert(sighting, at: 0)
            note("sighting: \(p.label.rawValue) from the widget while the app was closed")
            send(sighting)
        }
        sightings.sort { $0.seenAt > $1.seenAt }
        // Cleared only once they are safely in the sightings file; otherwise they wait for next time.
        if JSONStore.saveNow(sightings, "sightings.json") { MarkInbox.standard.clear() }
        recomputeSummary()
    }

    private func send(_ s: Sighting) {
        guard !demo else {
            update(s.id) { $0.phase = .saved; $0.detail = "Demo only, nothing was sent" }
            return
        }
        guard hasLink else { return }
        let note = [s.note, s.place?.noteText ?? ""].filter { !$0.isEmpty }.joined(separator: "; ")
        let json: [String: Any] = ["kind": "mark", "id": s.id, "label": s.label.rawValue, "note": String(note.prefix(500)),
                                   "age_s": max(0, Date().timeIntervalSince(s.seenAt)), "phone_utc": s.seenAt.timeIntervalSince1970]
        client.send(path: "api/op", json: json) { [weak self] result in
            switch result {
            case .success:
                self?.update(s.id) { if $0.phase == .sending { $0.phase = .waitingForPi; $0.detail = "Waiting for the Pi" } }
            case .failure(let error):
                // Anything but a refusal stays queued; resendPending tries again.
                if ServerClient.status(of: error) == 400 {
                    self?.update(s.id) { $0.phase = .failed; $0.detail = "The server refused it" }
                }
            }
        }
    }

    /// Keeps trying until the Pi has answered. Repeats are harmless: both ends ignore a known id.
    private func resendPending(_ now: Double) {
        guard now - lastResend >= 5, !demo, hasLink else { return }
        lastResend = now
        let listed = Set(tracker.frame?.message.ops?.map(\.id) ?? [])
        let waiting = sightings.filter { $0.needsSending && ($0.phase == .sending || !listed.contains($0.id)) }
        for s in waiting.prefix(50) { send(s) }
    }

    private func update(_ id: String, _ change: (inout Sighting) -> Void) {
        guard let index = sightings.firstIndex(where: { $0.id == id }) else { return }
        let before = sightings[index]
        change(&sightings[index])
        if sightings[index] != before { JSONStore.save(sightings, "sightings.json") }
    }

    func deleteSighting(_ id: String) {
        sightings.removeAll { $0.id == id }
        JSONStore.save(sightings, "sightings.json")
        recomputeSummary()
    }

    // MARK: Operations

    private func applyOps(_ ops: [OpStatus], command: CommandStatus?) {
        for op in ops {
            switch op.kind {
            case "mark":
                update(op.id) { $0.apply(state: op.state, detail: op.detail) }
            case "poweroff":
                if op.id == powerOff.id { powerOff = Self.actionState(op, waiting: "Waiting for the Pi…", done: "Powering off") }
            case "notify":
                if op.id == pushoverAction.id {
                    pushoverAction = Self.actionState(op, waiting: "Waiting for the car computer…",
                                                      done: op.detail.ifEmpty("Saved on the car computer"))
                }
            case "bluetooth":
                bluetooth.apply(op)
            case "doctor":
                if op.id == doctorAction.id {
                    doctorAction = Self.actionState(op, waiting: "The car computer is checking itself…", done: op.detail.ifEmpty("Checked"))
                }
            case "logs":
                if op.id == piLogs.id {
                    let done = op.detail.ifEmpty("Saved on the server")
                    if piLogs.busy && op.state == "done" { refreshServerLogs() }
                    piLogs = Self.actionState(op, waiting: "Waiting for the Pi…", done: done)
                }
            default:
                break
            }
        }
        if stopAction.id == "stop", stopAction.busy, tracker.detectorState == "stopped" {
            stopAction = .init(id: "stop", text: "Stopped. It starts again when the Pi next powers up.", level: .ok)
        } else if let command, stopAction.id == "stop", stopAction.busy {
            switch command.state {
            case "pending": stopAction = .init(id: "stop", text: "Waiting for the Pi to collect it…", level: .idle, busy: true)
            case "sent": stopAction = .init(id: "stop", text: "The Pi has it. It should stop within seconds.", level: .ok, busy: true)
            case "done": stopAction = .init(id: "stop", text: "Stopped. It starts again when the Pi next powers up.", level: .ok)
            case "expired": stopAction = .init(id: "stop", text: "The Pi did not collect it in time, so it was cancelled.", level: .bad)
            default: break
            }
        }
    }

    private static func actionState(_ op: OpStatus, waiting: String, done: String) -> ActionState {
        switch op.state {
        case "done": return .init(id: op.id, text: done, level: .ok)
        case "failed", "expired": return .init(id: op.id, text: op.detail.ifEmpty("It did not work"), level: .bad)
        default: return .init(id: op.id, text: waiting, level: .idle, busy: true)
        }
    }

    private func sendOp(kind: String, fields: [String: Any] = [:], update apply: @escaping (ActionState) -> Void) {
        let id = Sighting.newID()
        apply(.init(id: id, text: "Sending…", level: .idle, busy: true))
        var json = fields
        json["kind"] = kind
        json["id"] = id
        client.send(path: "api/op", json: json) { result in
            if case .failure(let error) = result {
                apply(.init(id: id, text: "Could not reach the server: \(error.localizedDescription)", level: .bad))
            }
        }
    }

    func requestPowerOff() {
        expectingSilence = true
        note("power off requested")
        sendOp(kind: "poweroff") { [weak self] in self?.powerOff = $0 }
    }

    /// Switches the car computer's Pushover messages on or off. A switch made while the car is off waits
    /// on the server for the next drive (a day at most).
    func setPushover(_ on: Bool) {
        guard pushoverUnavailable == nil else { return }
        pushoverWanted = on
        note("Pushover messages \(on ? "on" : "off") requested")
        sendOp(kind: "notify", fields: ["pushover": on]) { [weak self] in self?.pushoverAction = $0 }
    }

    /// The car computer's own report, or nil when it has not sent one.
    var pushoverOnCar: NotifySwitch? { tracker.frame?.message.state?.notify }

    // MARK: Updates

    /// The newest published release, as the server last heard from GitHub.
    var release: ReleaseInfo? { tracker.frame?.message.release }
    static var shortVersion: String { Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "" }
    /// A newer app than this one, or nil.
    var appUpdate: String? { Updates.newer(release?.app, than: Self.shortVersion) }
    /// Newer car computer software than it reports running, or nil.
    var piUpdate: String? { Updates.newer(release?.software, than: tracker.frame?.message.state?.software) }

    /// One notification per new version, and only where notifications are already allowed for the
    /// fault messages: the app never asks for permission just for this.
    private func announceUpdates() {
        guard !demo, let notices, noticePermission == "Allowed",
              let text = Updates.announcement(app: appUpdate, software: piUpdate) else { return }
        let key = "slot17.announcedUpdate"
        guard UserDefaults.standard.string(forKey: key) != text else { return }
        UserDefaults.standard.set(text, forKey: key)
        note("update available: \(text)")
        notices.post(.init(title: "Slot17: update available", body: "New: \(text). See Settings, About."))
    }

    /// The switch as shown: the request on its way, else the car's report, else on (the car's default).
    var pushoverShown: Bool { pushoverWanted ?? pushoverOnCar?.pushover ?? true }

    /// Why the switch cannot be used, or nil. The car's last report counts even when it is old: the server
    /// keeps the request until the next drive.
    var pushoverUnavailable: String? {
        if demo { return "Not available in demo mode" }
        if !hasLink { return "Add the private link first" }
        guard let state = tracker.frame?.message.state else { return "The car computer has not reported yet" }
        if !(state.features ?? []).contains("notify") { return "The car computer needs software 1.2.9 or later for this" }
        if state.notify?.configured == false { return "Pushover is not set up on the car computer" }
        return nil
    }

    /// Forgets the request once the car reports it, or once it failed, so the switch shows the truth.
    private func settlePushoverWanted() {
        guard let wanted = pushoverWanted, !pushoverAction.busy else { return }
        if pushoverOnCar?.pushover == wanted || pushoverAction.level == .bad { pushoverWanted = nil }
    }

    /// The Pi screen's setup check: this phone's side, then the car computer's own.
    var setupRows: [CheckRow] {
        SetupCheck.phone(hasLink: hasLink || demo, chain: chain, notifications: noticePermission, armed: audio.armed, sound: settings.sound)
    }
    var carRows: [CheckRow] { SetupCheck.car(piState?.doctor) }

    func requestDoctor() {
        note("setup check requested")
        sendOp(kind: "doctor") { [weak self] in self?.doctorAction = $0 }
    }

    func requestPiLogs() {
        note("Pi log requested")
        sendOp(kind: "logs") { [weak self] in self?.piLogs = $0 }
    }

    func requestStop() {
        expectingSilence = true
        note("detector stop requested")
        stopAction = .init(id: "stop", text: "Sending…", level: .idle, busy: true)
        client.send(path: "api/command", json: ["action": "stop", "note": "from the iPhone app"]) { [weak self] result in
            if case .failure(let error) = result {
                self?.stopAction = .init(id: "stop", text: "Could not reach the server: \(error.localizedDescription)", level: .bad)
            }
        }
    }

    // MARK: Logs

    func uploadAppLog() {
        logUpload = .init(text: "Sending…", busy: true)
        note("sending this log to the server")
        let header = (diagnostics.map { "\($0.0): \($0.1)" } + [""]).joined(separator: "\n")
        log.tail(maxBytes: 900_000) { [weak self] tail in
            var body = Data(header.utf8)
            body.append(tail)
            self?.sendAppLog(body)
        }
    }

    private func sendAppLog(_ body: Data) {
        client.uploadLog(body) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let data):
                let name = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["name"] as? String ?? "on the server"
                self.logUpload = .init(text: "Saved as \(name)", level: .ok)
                self.note("log saved on the server as \(name)")
                self.refreshServerLogs()
            case .failure(let error):
                self.logUpload = .init(text: "Could not send: \(error.localizedDescription)", level: .bad)
            }
        }
    }

    func refreshServerLogs() {
        client.listLogs { [weak self] result in
            switch result {
            case .success(let logs):
                self?.serverLogs = logs
                self?.serverLogsNote = logs.isEmpty ? "Nothing saved on the server yet" : ""
            case .failure(let error):
                self?.serverLogsNote = "Could not list them: \(error.localizedDescription)"
            }
        }
    }

    func fetchServerLog(_ name: String, done: @escaping (String) -> Void) {
        client.fetchLog(name) { result in
            switch result {
            case .success(let text): done(text)
            case .failure(let error): done("Could not load it: \(error.localizedDescription)")
            }
        }
    }

    func clearLocalLog() {
        log.clear()
        note("log cleared")
    }

    func clearAlertHistory() {
        alerts = []
        saveHistory()
        recomputeSummary()
    }

    private func note(_ text: String) {
        log.write(text)
    }

    // MARK: Demo

    /// One channel slowly strengthens, as in the web page's demo. Never sounds like real data
    /// unasked: the screen carries a banner, and the alert rule still applies.
    private func demoMessage(_ now: Double) -> ServerMessage {
        let t = (now - demoStart).truncatingRemainder(dividingBy: 70)
        let strength = (t < 50 ? 8 + t * 0.55 : 35.5 - (t - 50) * 1.2)
        let beat = Int(t / 4)
        var matches: [MatchRow] = []
        if beat != demoBeat { matches = [MatchRow(id: "demo/\(Int(now * 1000))", ch: 154, strength: strength, age: 0.4)] }
        demoBeat = beat
        demoSeq += 1
        let isScreenshot = ProcessInfo.processInfo.arguments.contains("--screenshot-values")
        let first = isScreenshot ? 55.0 : strength
        return ServerMessage(
            boot: "demo", seq: demoSeq, serverMono: now, serverAge: 0,
            state: PiState(
                source: "demo", session: "demo", snapshotAge: 0,
                detector: DetectorState(state: "running", windows: 412, hits: 128, unchecked: 6, partial: 3, quietWindows: 0, inputPeak: 1840),
                channels: [
                    ChannelRow(ch: 154, freqMHz: 383.8625, strength: first, age: 1, hits60: 14, approxTime: false,
                               activity: "active"),
                    ChannelRow(ch: 60, freqMHz: 381.5125, strength: isScreenshot ? 9.8 : 14.2, age: 9, hits60: 3,
                               approxTime: false),
                    ChannelRow(ch: 119, freqMHz: 382.9875, strength: nil, age: 31, hits60: 1, approxTime: true),
                ],
                matches: isScreenshot ? [] : matches, features: [],
                pi: PiHealth(uptime: 1240, temperature: 52.4, load: 1.8, diskFreeGB: 96.2, memFreeMB: 6120,
                             sessionSeconds: 1236, droppedSamples: 0)),
            command: nil, ops: nil)
    }
}
