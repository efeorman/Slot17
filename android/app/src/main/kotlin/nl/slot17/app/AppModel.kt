package nl.slot17.app

import android.content.Context
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.max
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import nl.slot17.core.AlertDecision
import nl.slot17.core.BluetoothFile
import nl.slot17.core.BluetoothView
import nl.slot17.core.AlertEngine
import nl.slot17.core.AlertRecord
import nl.slot17.core.AlertSettings
import nl.slot17.core.ChainItem
import nl.slot17.core.CommandStatus
import nl.slot17.core.FaultNotices
import nl.slot17.core.Fmt
import nl.slot17.core.HeadlineStatus
import nl.slot17.core.InterferenceMeter
import nl.slot17.core.Level
import nl.slot17.core.LinkTracker
import nl.slot17.core.MatchKey
import nl.slot17.core.RouteStats
import nl.slot17.core.MarkSummary
import nl.slot17.core.MeterRow
import nl.slot17.core.Meters
import nl.slot17.core.OpStatus
import nl.slot17.core.PiState
import nl.slot17.core.ProtocolJson
import nl.slot17.core.ReleaseInfo
import nl.slot17.core.RemoteConfig
import nl.slot17.core.ServerLink
import nl.slot17.core.ServerLogEntry
import nl.slot17.core.ServerMessage
import nl.slot17.core.Sighting
import nl.slot17.core.CarMessages
import nl.slot17.core.SetupCheck
import nl.slot17.core.CheckRow
import nl.slot17.core.SoundKind
import nl.slot17.core.SoundSynth
import nl.slot17.core.StatusBuilder
import nl.slot17.core.Updates
import nl.slot17.core.ChannelRow
import nl.slot17.core.DetectorState
import nl.slot17.core.MatchRow
import nl.slot17.core.NotifySwitch
import nl.slot17.core.PiHealth

/** The result of an action on the Pi or the server, as one line under its button. */
data class ActionState(val id: String = "", val text: String = "", val level: Level = Level.IDLE, val busy: Boolean = false)

/** Meter colours and words from the server's settings. Port of DisplayStyle.swift. */
data class DisplayStyle(
    val weak: String = "#6F8BA8",
    val silent: String = "#F5C542",
    val chime: String = "#FF4D4F",
    val strongOutline: String = "#FFFFFF",
    val strongBadge: String = "STRONG",
    val frequentText: String = "Frequent bursts",
    val pulseOnNewStrong: Boolean = true,
    val tileGap: Double = 10.0,
    val floorQuiet: Double? = null,
) {
    companion object {
        fun from(d: RemoteConfig.Display?): DisplayStyle {
            val s = DisplayStyle()
            d ?: return s
            return DisplayStyle(d.weakColour ?: s.weak, d.silentColour ?: s.silent, d.chimeColour ?: s.chime,
                d.strongOutlineColour ?: s.strongOutline, d.strongBadge ?: s.strongBadge, d.frequentText ?: s.frequentText,
                d.pulseOnNewStrong ?: s.pulseOnNewStrong, d.tileGapS ?: s.tileGap, d.floorQuietDB)
        }
    }
}

enum class Tab(val title: String) { LIVE("Live"), SIGHTINGS("Sightings"), PI("Pi"), LOGS("Logs"), SETTINGS("Settings") }

/**
 * Everything the screens show and every rule the app follows. A port of ios/Slot17/Services/AppModel.swift,
 * keeping its order of checks, its words and its timings, so both apps behave the same in the car.
 * All of it runs on the main thread; the network work runs elsewhere and reports back here.
 */
class AppModel(private val context: Context) {
    private val main = Handler(Looper.getMainLooper())
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    val store = Store(context)
    val log = EventLog(java.io.File(context.filesDir, "app.log"))
    val audio = SoundPlayer(context) { note(it) }
    val location = LocationTracker(context, store) { note(it) }
    val notices = Notices(context)
    private val car = AutoMessages(context)
    private val client = ServerClient(scope, ::received, ::note, ::unauthorised)
    private val tracker = LinkTracker()
    private var engine = AlertEngine()
    private val interference = InterferenceMeter()
    private val faultNotices = FaultNotices()
    /** The Bluetooth route to the car computer, beside the server (1.4.0). */
    val bluetooth = BluetoothRoute(context, store, sendOp = { kind, fields, apply -> sendOp(kind, fields, apply) },
        carState = { carStateNow }, deliver = ::bluetoothMatches, changed = ::refresh, note = ::note,
        carStateAge = { carStateAge }, answered = ::bluetoothAnswers, fileArrived = ::bluetoothFile)
    /** Support packages from the Pi screen (1.5.0). */
    val support = SupportRoute(context, scope, client, sendOp = { kind, fields, apply -> sendOp(kind, fields, apply) },
        carState = { if (demo) null else carStateNow }, phoneFiles = ::supportPhoneFiles, note = ::note)

    // What the screens show
    var tab by mutableStateOf(Tab.LIVE)
    var headline by mutableStateOf(HeadlineStatus(Level.IDLE, "Starting…", ""))
        private set
    var chain by mutableStateOf(emptyList<ChainItem>())
        private set
    var meters by mutableStateOf(emptyList<MeterRow>())
        private set
    var extraMeters by mutableStateOf(emptyList<MeterRow>())
        private set
    var moreChannels by mutableStateOf(0)
        private set
    var showBars by mutableStateOf(false)
        private set
    var receiverQuiet by mutableStateOf(false)
        private set
    var mutedSeconds by mutableStateOf(0)
        private set
    var soundLine by mutableStateOf("")
        private set
    var soundOK by mutableStateOf(false)
        private set
    var sightings by mutableStateOf(store.load("sightings.json", ListSerializer(Sighting.serializer())) ?: emptyList())
        private set
    var alerts by mutableStateOf(store.load("alerts.json", ListSerializer(AlertRecord.serializer())) ?: emptyList())
        private set
    var logLines by mutableStateOf(emptyList<String>())
        private set
    var serverLogs by mutableStateOf(emptyList<ServerLogEntry>())
        private set
    var serverLogsNote by mutableStateOf("")
        private set
    var logUpload by mutableStateOf(ActionState())
        private set
    var piLogs by mutableStateOf(ActionState())
        private set
    var powerOff by mutableStateOf(ActionState())
        private set
    var stopAction by mutableStateOf(ActionState())
        private set
    var pushoverAction by mutableStateOf(ActionState())
    var doctorAction by mutableStateOf(ActionState())
        private set
    var pushoverWanted by mutableStateOf<Boolean?>(null)
        private set
    var diagnostics by mutableStateOf(emptyList<Pair<String, String>>())
        private set
    var hasLink by mutableStateOf(false)
        private set
    var linkHost by mutableStateOf("")
        private set
    var linkIsHttps by mutableStateOf(false)
        private set
    var banner by mutableStateOf("")
        private set
    var style by mutableStateOf(DisplayStyle())
        private set
    var configStatus by mutableStateOf("Built-in settings")
        private set
    var markSummary by mutableStateOf(MarkSummary())
        private set
    var driveUpload by mutableStateOf(ActionState())
        private set
    var piState by mutableStateOf<PiState?>(null)
        private set
    /** Pi logs that came over Bluetooth (1.6.0), newest first. Declared before init, which loads them. */
    var phonePiLogs by mutableStateOf(emptyList<PhoneLog>())
        private set
    var lastMessage by mutableStateOf<ServerMessage?>(null)
        private set
    var locationStatus by mutableStateOf("Off")
        private set
    var noticePermission by mutableStateOf(notices.permission)
        private set
    var serviceRunning by mutableStateOf(false)
    var settings by mutableStateOf(store.load("settings.json", AlertSettings.serializer()) ?: AlertSettings())
        private set
    var demo by mutableStateOf(false)
        private set
    /** The in-app update, as words for the Settings screen; null when none is going on. */
    var updateState by mutableStateOf<String?>(null)
    val updater = AppUpdater(context)
    /** The wall clock in whole seconds, so "12 s ago" lines move on by themselves. */
    var nowMs by mutableStateOf(System.currentTimeMillis())
        private set
    /** Whole-band interference in about the last minute, as a percent of the detector's windows. */
    var interferencePercent by mutableStateOf<Double?>(null)
        private set

    private var applyingRemote = false
    private var remote: RemoteConfig? = null
    private var remoteVersion: String? = null
    private var soundRetryNeeded = false
    private var lastConfigFetch = Double.NEGATIVE_INFINITY
    private var carGoneSince: Double? = null
    private var driveUploadDoneThisStop = false
    private var mutedUntil = Double.NEGATIVE_INFINITY
    private var lastProbeAt = Double.NEGATIVE_INFINITY
    private var probing = false
    private var lastAuthRetry = Double.NEGATIVE_INFINITY
    private var lastTick = mono()
    private var suspensions = 0
    private var messages = 0
    private var lastQuietTone = Double.NEGATIVE_INFINITY
    private var wasLiveAt: Double? = null
    private var linkToneOutageDone = false
    private var asleep = false
    private var driveRetryAt = Double.NEGATIVE_INFINITY
    private var linkGeneration = 0
    private var lastCarLiveAt = Double.NEGATIVE_INFINITY
    private var tickInterval = 0.25
    private var linkWarningRefusalNoted = false
    private var listeningSince = mono()
    private var expectingSilence = false
    private var noDataSince: Double? = null
    private var demoStart = mono()
    private var demoSeq = 0
    private var demoBeat = 0
    private var lastResend = Double.NEGATIVE_INFINITY
    private var lastHistorySave = mono()
    private var historyDirty = false
    private var listening = false
    private var visible = false
    private var lastLagNote = Double.NEGATIVE_INFINITY
    private var lastNotice: LiveNotice? = null
    private var lastNoticeAt = Double.NEGATIVE_INFINITY
    private var lastWidgetLine = ""
    private var lastAlertIdShown: String? = null

    private val tickRunnable = object : Runnable {
        override fun run() {
            tick()
            main.postDelayed(this, (tickInterval * 1000).toLong())
        }
    }

    init {
        loadPhonePiLogs()
        audio.onPlayed = { kind, peak -> note("sound played: ${kind.name.lowercase()}, peak ${Fmt.fixed(peak, 1)} dBFS, output ${audio.routeName}") }
        location.onChange = { locationStatus = location.statusText }
        location.trackEvery = settings.trackEvery
        applyAudioSettings()
        note("app started, version $version; battery $batteryText")
        loadLink()
        loadCachedConfig()
        recomputeSummary()
        refresh()
    }

    // Opening and closing

    /** Opened by the person: everything that listens and makes sound starts now. */
    fun becameVisible() {
        visible = true
        note("app opened; output ${audio.routeName}")
        noticePermission = notices.permission
        asleep = false
        noDataSince = null
        tickInterval = 0.25
        if (!listening) {
            listening = true
            listeningSince = mono()
            lastTick = mono()
            audio.arm()
            if (settings.recordLocation) location.start()
            if (!demo) connect()
            main.post(tickRunnable)
        } else {
            if (!audio.armed) audio.arm()
            if (settings.recordLocation) location.start()
            if (!demo && hasLink && !tracker.authFailed && mono() - tracker.rxAt > LinkTracker.LINK_LOST) connect()
        }
        sendLeftoverDriveRecords()
        LinkService.start(context)
        bluetooth.start()
        refresh()
    }

    fun becameHidden() {
        visible = false
        tickInterval = 1.0
        saveHistory()
        location.saveTrack()
        note("app moved to the background; output ${audio.routeName}")
    }

    // Connection

    private fun loadLink() {
        val link = store.link?.let(ServerLink::parse)
        client.configure(link)
        hasLink = link != null
        linkHost = link?.base?.substringAfter("://") ?: ""
        linkIsHttps = link?.base?.startsWith("https") == true
    }

    /** Returns a reason if the text is not a private link. */
    fun setLink(text: String): String? {
        if (ServerLink.parse(text) == null) return "That is not a private link. It looks like http://address:port/?key=..."
        val previous = linkHost
        store.link = text.trim()
        loadLink()
        if (linkHost != previous) forgetServerSettings()
        resetLink()
        connect()
        note("private link changed; server $linkHost")
        refresh()
        return null
    }

    fun removeLink() {
        store.link = null
        client.closeStream()
        loadLink()
        resetLink()
        refresh()
    }

    fun switchDemo(on: Boolean) {
        if (on == demo) return
        demo = on
        resetLink()
        tracker.demo = on
        demoStart = mono()
        if (on) client.closeStream() else connect()
        if (on && !listening) becameVisible()
        refresh()
    }

    /** Another server's rules, words and sounds must not stay in use on this one. */
    private fun forgetServerSettings() {
        store.delete(CONFIG_CACHE)
        remote = null
        remoteVersion = null
        soundRetryNeeded = false
        store.prefs.edit().putLong("lastDriveUpload", System.currentTimeMillis()).apply()
        sightings = sightings.map { if (it.needsSending) it.copy(phase = Sighting.Phase.FAILED, detail = "Not sent: the private link changed") else it }
        saveSightings()
        applyingRemote = true
        setSettingsInternal(settings.restoreBuiltInRules())
        applyingRemote = false
        banner = ""
        style = DisplayStyle()
        for (kind in SOUND_KINDS) audio.setSound(kind, SoundPlayer.Sound(SoundSynth.render(kind.builtIn), 44_100))
        configStatus = "Built-in settings"
    }

    private fun resetLink() {
        linkGeneration++
        probing = false
        tracker.reset()
        engine = AlertEngine()
        wasLiveAt = null
        linkToneOutageDone = false
        expectingSilence = false
        lastProbeAt = Double.NEGATIVE_INFINITY
    }

    private fun connect() {
        if (!hasLink || demo) return
        client.openStream()
        probe()
        fetchConfig()
    }

    // Settings from the server

    private fun loadCachedConfig() {
        val decoded = store.readText(CONFIG_CACHE)?.let(RemoteConfig::decode) ?: return
        if (decoded.second.problems.isEmpty()) applyRemote(decoded.first, decoded.second, "saved copy")
    }

    fun fetchConfig() {
        if (!hasLink || demo) return
        lastConfigFetch = mono()
        val generation = linkGeneration
        scope.launch {
            val result = client.fetchConfig()
            if (generation != linkGeneration) return@launch
            result.onSuccess { text ->
                val decoded = RemoteConfig.decode(text)
                if (decoded == null) {
                    configStatus = "Server settings unreadable; keeping the current ones"
                    note("PROBLEM server settings could not be read")
                    return@onSuccess
                }
                val problems = decoded.second.problems
                if (problems.isNotEmpty()) {
                    configStatus = "Server settings refused: ${problems.take(2).joinToString("; ")}"
                    note("PROBLEM server settings refused: ${problems.joinToString("; ")}")
                    return@onSuccess
                }
                if (decoded.first == remoteVersion && !soundRetryNeeded) return@onSuccess
                store.writeText(CONFIG_CACHE, text)
                applyRemote(decoded.first, decoded.second, "server")
            }.onFailure {
                if (ServerClient.status(it) == 404 && remote == null) configStatus = "Built-in settings (none on the server)"
            }
        }
    }

    private fun applyRemote(version: String, config: RemoteConfig, source: String) {
        remote = config
        remoteVersion = version
        applyingRemote = true
        setSettingsInternal(settings.apply(config))
        applyingRemote = false
        banner = config.message ?: ""
        soundRetryNeeded = false
        style = DisplayStyle.from(config.display)
        for (kind in SOUND_KINDS) {
            val name = if (kind == SoundKind.LINK_LOST) "link_lost" else kind.name.lowercase()
            val spec = config.sounds?.get(name) ?: kind.builtIn
            val wav = spec.wav
            if (wav != null) loadServerSound(kind, wav) else audio.setSound(kind, SoundPlayer.Sound(SoundSynth.render(spec), 44_100))
        }
        configStatus = if (source == "server") "Server, version $version" else "Saved copy of server version $version"
        note("settings $version applied from the $source" +
            if (settings.overrides.isEmpty()) "" else "; kept your own: ${settings.overrides.sorted().joinToString(", ")}")
    }

    private fun loadServerSound(kind: SoundKind, name: String) {
        val generation = linkGeneration
        scope.launch {
            val result = client.fetchSound(name)
            if (generation != linkGeneration) return@launch
            val sound = result.getOrNull()?.let(SoundPlayer::wav)
            if (sound != null) {
                audio.setSound(kind, sound)
                note("sound ${kind.name.lowercase()} now plays $name from the server")
            } else {
                soundRetryNeeded = true
                note("PROBLEM sound $name could not be used; ${kind.name.lowercase()} keeps its current sound for now")
            }
        }
    }

    /** Drops the operator's own changes to rules, so the server's (or built-in) values apply again. */
    fun useServerSettings() {
        applyingRemote = true
        val d = AlertSettings()
        var reset = settings.copy(overrides = emptySet(), red = d.red, yellow = d.yellow, quietRed = d.quietRed, quietGap = d.quietGap)
        remote?.let { reset = reset.apply(it) }
        setSettingsInternal(reset)
        applyingRemote = false
        note("own rule changes cleared; using ${if (remote == null) "built-in" else "server"} settings")
    }

    /** A change from the Settings screen: remembered as the operator's own, so the server leaves it alone. */
    fun updateSettings(change: (AlertSettings) -> AlertSettings) = setSettingsInternal(change(settings))

    private fun setSettingsInternal(new: AlertSettings) {
        val old = settings
        if (new == old) return
        var s = new
        if (!applyingRemote) s = s.copy(overrides = s.overrides + AlertSettings.changedKeys(old, s))
        settings = s
        store.save("settings.json", AlertSettings.serializer(), s)
        applyAudioSettings()
        location.trackEvery = s.trackEvery
        if (s.recordLocation != old.recordLocation) { if (s.recordLocation && listening) location.start() else location.stop() }
        if (s.summaryWindow != old.summaryWindow) recomputeSummary()
        if (!s.liveActivity && old.liveActivity) lastNotice = null
        refresh()
    }

    // Messages

    private fun probe() {
        if (probing || demo || !hasLink) return
        probing = true
        lastProbeAt = mono()
        val generation = linkGeneration
        scope.launch {
            val p = client.probe()
            if (generation != linkGeneration) return@launch
            probing = false
            p ?: return@launch
            val m = p.message
            if (m != null) {
                handleAcceptance(tracker.applyProbe(m, p.begin, p.end), m)
                refresh()
            } else if (ServerClient.status(p.error) == 401) {
                unauthorised()
            }
        }
    }

    private fun unauthorised() {
        if (tracker.authFailed) return
        tracker.authFailed = true
        engine.clearWaiting()
        client.closeStream()
        note("PROBLEM the server refused the private link")
        refresh()
    }

    private fun received(message: ServerMessage) {
        if (demo) return
        handleAcceptance(tracker.accept(message, mono()), message)
        if (message.release != null) announceUpdates()
        if (!tracker.clockReady(mono()) && mono() - lastProbeAt >= 2) probe()
        refresh()
        if (listening) updateNotice(mono())
    }

    private fun handleAcceptance(result: LinkTracker.Acceptance, message: ServerMessage) {
        if (result == LinkTracker.Acceptance.REFUSED) return
        messages++
        if (result == LinkTracker.Acceptance.FRESH_AND_NEW_SCOPE) engine.resetContext()
        if (result == LinkTracker.Acceptance.FRESH || result == LinkTracker.Acceptance.FRESH_AND_NEW_SCOPE) {
            val d = message.state?.detector
            if (d?.windows != null && d.wholeBandWindows != null) {
                interference.add(mono(), d.windows!!, d.wholeBandWindows!!)
                interferencePercent = interference.percent
            }
            serverMatches(message.state?.matches ?: emptyList())
            engine.ingest(message.state?.matches ?: emptyList(), tracker.frame, message.state?.channels ?: emptyList(), tracker.scope)
        }
        flushMatches()
        applyOps(message.ops ?: emptyList(), message.command)
    }

    /** Matches from the server: counted for the route figures, and an alert first heard over Bluetooth takes the server's id. */
    private fun serverMatches(matches: List<MatchRow>) {
        val now = mono()
        for (m in matches) {
            val key = MatchKey.of(m)
            bluetooth.stats.arrived(key, RouteStats.Route.SERVER, now)
            val i = alerts.indexOfFirst { it.id == "bt:$key" }
            if (i >= 0) {
                alerts = alerts.toMutableList().also { it[i] = it[i].copy(id = m.id) }
                historyDirty = true
            }
        }
    }

    /** Matches over Bluetooth: their age counts from when they arrived, so no server clock is needed. */
    private fun bluetoothMatches(matches: List<MatchRow>, receivedAt: Double) {
        engine.ingest(matches, null, bluetooth.feed.channels, tracker.scope, receivedAt)
        flushMatches()
        refresh()
    }

    private fun flushMatches() {
        val now = mono()
        val context = AlertEngine.Context(
            now = now, live = tracker.live(now) || bluetooth.feed.live(now), mayHold = tracker.mayHoldMatches(now), scope = tracker.scope,
            settings = settings, armed = audio.armed, audioProblem = audio.problem, mutedUntil = mutedUntil,
            age = { w ->
                w.receivedAt?.let { w.match.age + (now - it) }
                    ?: w.frame?.let { w.match.age + tracker.elapsed(it, now) } ?: Double.POSITIVE_INFINITY
            },
            play = { audio.play(it) },
        )
        for (decision in engine.flush(context)) record(decision)
    }

    private fun record(d: AlertDecision) {
        var sound: SoundKind? = null
        var reason = ""
        when (val o = d.outcome) {
            is AlertDecision.Outcome.Played -> {
                sound = o.kind
                note("${o.kind.name.lowercase()} for L${d.channel}, ${db(d.strength)}, ${Fmt.fixed(d.age, 1)} s old")
                if (o.kind == SoundKind.URGENT && settings.carMessages && !demo) {
                    car.post(CarMessages.text(d.channel, d.strength), mono(), notices.allowed)
                        ?.takeIf { it != AutoMessages.NOT_CONNECTED }?.let { note("android auto: not shown, $it") }
                }
            }
            is AlertDecision.Outcome.Silent -> {
                reason = o.reason
                note("silent for L${d.channel}, ${db(d.strength)}: ${o.reason}")
            }
        }
        val record = AlertRecord(d.id, System.currentTimeMillis(), d.channel, d.strength, d.age, sound, reason,
            if (settings.recordLocation) location.place else null, d.rule)
        alerts = (listOf(record) + alerts).take(1000)
        historyDirty = true
        if (sound != null) recomputeSummary()
    }

    private fun recomputeSummary() {
        markSummary = MarkSummary.make(alerts, sightings, settings.summaryWindow)
    }

    // The regular tick

    private fun tick() {
        val now = mono()
        if (asleep) { lastTick = now; return }
        val late = now - lastTick
        if (late > tickInterval + 1 && late <= 5 && now - lastLagNote >= 60) {
            lastLagNote = now
            note("app lagged: ${Fmt.fixed(late, 1)} s between ticks")
        }
        if (now - lastTick > 5) {
            suspensions++
            note("PROBLEM Android stopped this app for ${Fmt.fixed(now - lastTick, 0)} s")
        }
        lastTick = now
        if (demo) {
            val message = demoMessage(now)
            handleAcceptance(tracker.accept(message, now), message)
        }
        watchdog(now)
        flushMatches()
        checkEquipmentTones(now)
        checkAutoSleep(now)
        checkDriveEnded(now)
        bluetooth.stats.settle(now)
        checkBluetoothSent(now)
        updateLocationPower(now)
        resendPending(now)
        checkFaultNotices(now)
        if (hasLink && !demo && now - lastConfigFetch >= 300) fetchConfig()
        if (historyDirty && now - lastHistorySave > 20) saveHistory()
        refresh()
        updateNotice(now)
    }

    private fun watchdog(now: Double) {
        if (!hasLink || demo) return
        if (tracker.authFailed) {
            if (now - lastAuthRetry > 15) { lastAuthRetry = now; retryRefusedLink() }
            return
        }
        val silent = if (tracker.frame != null) now - tracker.rxAt else now - client.openedAt
        if (silent > LinkTracker.REOPEN_AFTER && now - client.openedAt > LinkTracker.REOPEN_AFTER) client.openStream()
        if (now - lastProbeAt >= LinkTracker.CLOCK_REFRESH) probe()
    }

    /** While the key is refused, ask again in case it starts working, without any stream. */
    private fun retryRefusedLink() {
        val generation = linkGeneration
        scope.launch {
            val p = client.probe() ?: return@launch
            val m = p.message ?: return@launch
            if (asleep || generation != linkGeneration) return@launch
            tracker.authFailed = false
            handleAcceptance(tracker.applyProbe(m, p.begin, p.end), m)
            client.openStream()
            note("the server accepts the private link again")
        }
    }

    private fun checkEquipmentTones(now: Double) {
        if (demo || !audio.armed || !settings.sound || now < mutedUntil) return
        // Live by either route: with the server gone and Bluetooth live there is nothing to warn about (1.6.0).
        val live = tracker.live(now) || bluetooth.feed.live(now)
        if (live) { wasLiveAt = now; linkToneOutageDone = false; expectingSilence = false }
        // A stop asked for is expected silence. One by itself (1.6.0 car computers say why) is not: the warning plays.
        val detectorNow = if (serverCarFresh(now)) tracker.frame?.message?.state?.detector else bluetooth.feed.currentDetector(now)
        if (detectorNow?.state == "stopped" && detectorNow?.reasons.isNullOrEmpty()) expectingSilence = true
        val last = wasLiveAt
        if (settings.linkLostTone && last != null && !live && !expectingSilence && !linkToneOutageDone && now - last >= settings.linkLostAfter) {
            playLinkWarning("link lost for ${Fmt.fixed(settings.linkLostAfter, 0)} s: playing the warning, silence no longer means anything")
        }
        // Opened in the car, and nothing has ever arrived: as silent as a quiet road, so it must be said.
        if (settings.linkLostTone && wasLiveAt == null && !live && !expectingSilence && !linkToneOutageDone && audio.routeIsCar &&
            now - listeningSince >= max(settings.linkLostAfter, 60.0)) {
            playLinkWarning("never connected since the app was opened: playing the warning, silence means nothing")
        }
        if (StatusBuilder.receiverQuiet(tracker, settings, now, bluetoothView(now)) && now - lastQuietTone >= settings.receiverToneRepeat) {
            lastQuietTone = now
            note("receiver hears nothing: playing the warning")
            audio.play(SoundKind.WARNING)
        }
    }

    private fun checkFaultNotices(now: Double) {
        if (!settings.faultNotices || demo) return
        val d = piState?.detector
        val input = FaultNotices.Input(d?.state, d?.reasons ?: emptyList(), receiverQuiet, expectingSilence)
        faultNotices.step(input, now)?.let { notices.postFault(it) }
    }

    /** Counts as done only if the sound really started; otherwise the next tick tries again. */
    private fun playLinkWarning(text: String) {
        val refused = audio.play(SoundKind.LINK_LOST)
        if (refused != null) {
            if (!linkWarningRefusalNoted) { linkWarningRefusalNoted = true; note("PROBLEM the link warning could not play: $refused") }
        } else {
            linkToneOutageDone = true
            linkWarningRefusalNoted = false
            note(text)
        }
    }

    /** After parking, stop listening so the phone can rest. Opening the app starts it all again. */
    private fun checkAutoSleep(now: Double) {
        if (settings.autoSleepMinutes <= 0 || demo) return
        val carLive = tracker.frame != null && tracker.carAge(now) <= LinkTracker.CAR_STALE && tracker.detectorActive ||
            bluetooth.feed.live(now)
        if (carLive || visible) {
            noDataSince = null
            if (!audio.armed && visible) audio.arm()
            return
        }
        if (!audio.armed) return
        val since = noDataSince ?: now
        noDataSince = since
        if (now - since >= settings.autoSleepMinutes * 60.0) goToSleep("no car data for ${settings.autoSleepMinutes} min with the app closed")
    }

    private fun goToSleep(why: String) {
        asleep = true
        note("going to sleep: $why")
        audio.disarm(why)
        location.stop()
        client.closeStream()
        saveHistory()
        LinkService.stop(context)
        bluetooth.stop()
        refresh()
    }

    private fun updateLocationPower(now: Double) {
        val carLive = tracker.frame != null && tracker.carAge(now) <= LinkTracker.CAR_STALE || bluetooth.feed.live(now)
        if (carLive) lastCarLiveAt = now
        location.setLowPower(!visible && !carLive && now - max(lastCarLiveAt, listeningSince) >= 120)
    }

    // Drive data

    private fun checkDriveEnded(now: Double) {
        if (!hasLink || demo) return
        val carLive = tracker.frame != null && tracker.carAge(now) <= LinkTracker.CAR_STALE || bluetooth.feed.live(now)
        if (carLive) { carGoneSince = null; driveUploadDoneThisStop = false; return }
        if (tracker.frame == null || driveUploadDoneThisStop) return
        val since = carGoneSince ?: now
        carGoneSince = since
        if (now - since >= 180 && now >= driveRetryAt && !driveUpload.busy && hasNewDriveData) {
            driveUploadDoneThisStop = true
            sendDriveRecords()
        }
    }

    private val lastDriveUpload: Long get() = store.prefs.getLong("lastDriveUpload", 0L)

    private val hasNewDriveData: Boolean
        get() = alerts.any { it.atMs > lastDriveUpload } || sightings.any { it.seenAtMs > lastDriveUpload }

    private fun sendDriveRecords() { uploadDriveData(automatic = true); uploadAppLog() }

    private fun sendLeftoverDriveRecords() {
        if (!hasLink || demo || !hasNewDriveData || driveUpload.busy) return
        val newest = listOfNotNull(alerts.firstOrNull()?.atMs, sightings.firstOrNull()?.seenAtMs).maxOrNull() ?: 0L
        if (System.currentTimeMillis() - newest >= 600_000) {
            note("sending the records of the last drive, which were not sent before")
            sendDriveRecords()
        }
    }

    /** Alerts, sightings and the location track since the last upload, as one JSON file on the server. */
    fun uploadDriveData(automatic: Boolean = false) {
        val from = lastDriveUpload
        val everything = System.currentTimeMillis()
        val waitingAlerts = alerts.filter { it.atMs > from }.sortedBy { it.atMs }
        val waitingTrack = location.track.filter { it.atMs > from }.sortedBy { it.atMs }
        val recentSightings = sightings.filter { it.seenAtMs > from - 3_600_000 }
        var until = everything
        var sent = DriveData.Counts(waitingAlerts.size, recentSightings.size, waitingTrack.size)
        var body = DriveData.encode(version, from, until, settings, waitingAlerts, recentSightings, waitingTrack)
        // The server takes at most 1 MB. Too much goes in parts, oldest first, so no record is ever skipped.
        var limit = 800
        while (body.size > 950_000 && limit >= 25) {
            val part = waitingAlerts.take(limit)
            val track = waitingTrack.take(limit * 5)
            val oldestFirst = recentSightings.sortedBy { it.seenAtMs }
            val marks = oldestFirst.take(limit)
            val ends = listOfNotNull(
                if (part.size < waitingAlerts.size) part.lastOrNull()?.atMs else null,
                if (track.size < waitingTrack.size) track.lastOrNull()?.atMs else null,
                if (marks.size < oldestFirst.size) marks.lastOrNull()?.seenAtMs else null,
            )
            until = ends.minOrNull() ?: everything
            val a = part.filter { it.atMs <= until }
            val s = marks.filter { it.seenAtMs <= until }
            val t = track.filter { it.atMs <= until }
            sent = DriveData.Counts(a.size, s.size, t.size)
            body = DriveData.encode(version, from, until, settings, a, s, t)
            limit /= 2
        }
        val partial = until < everything
        val sentUntil = until
        val counts = sent
        driveUpload = ActionState(text = "Sending…", busy = true)
        scope.launch {
            client.uploadLog(body).onSuccess { reply ->
                val name = nameFrom(reply)
                store.prefs.edit().putLong("lastDriveUpload", sentUntil).apply()
                if (partial) driveUploadDoneThisStop = false
                driveUpload = ActionState(text = "${if (automatic) "Sent automatically" else "Sent"} as $name${if (partial) ", more to come" else ""}", level = Level.OK)
                note("drive data sent as $name (${counts.alerts} alerts, ${counts.sightings} sightings, ${counts.places} places)${if (partial) ", more to come" else ""}")
                refreshServerLogs()
            }.onFailure {
                driveUpload = ActionState(text = "Could not send: ${it.message}. It stays on this phone and will be tried again.", level = Level.BAD)
                if (automatic) { driveUploadDoneThisStop = false; driveRetryAt = mono() + 120 }
            }
        }
    }

    private fun saveHistory() {
        historyDirty = false
        lastHistorySave = mono()
        store.save("alerts.json", ListSerializer(AlertRecord.serializer()), alerts)
    }

    private fun saveSightings() = store.save("sightings.json", ListSerializer(Sighting.serializer()), sightings)

    // What the screens show

    private fun refresh() {
        val now = mono()
        nowMs = System.currentTimeMillis() / 1000 * 1000
        val view = bluetoothView(now)
        headline = StatusBuilder.headline(tracker, now, engine.lastDelay, hasLink || demo, view)
        chain = StatusBuilder.chain(tracker, now, hasLink || demo,
            StatusBuilder.quietLimit(view?.detector ?: tracker.frame?.message?.state?.detector, settings), view)
        val frame = tracker.frame
        val state = frame?.message?.state
        lastMessage = frame?.message
        val serverFresh = serverCarFresh(now)
        // Without the server, the car computer's own state over Bluetooth: from 1.6.0 car computers all of it, from
        // older ones the detector and channels on top of the server's last report.
        val overBluetooth = !serverFresh && bluetooth.feed.live(now)
        piState = if (serverFresh) state else if (bluetooth.feed.heard(now)) bluetooth.feed.piState(state, now) else null
        val carFresh = frame != null && tracker.clockReady(now) && tracker.carAge(now) <= LinkTracker.CAR_STALE &&
            tracker.sinceRx(now) <= LinkTracker.LINK_LOST
        val bars = carFresh && tracker.detectorActive
        showBars = bars || overBluetooth
        var rows = emptyList<MeterRow>()
        if (bars && frame != null && state != null) {
            val elapsed = tracker.elapsed(frame, now)
            rows = Meters.pick(state.channels.map {
                // Ages in half seconds: the screen shows whole seconds, and finer steps only redraw.
                val age = floor((it.age + elapsed) * 2) / 2
                MeterRow(it.ch, it.freqMHz, it.strength, age, it.hits60, it.approxTime ?: false,
                    frequent = it.activity == "active" && age < 15, rising = (it.rising ?: false) && age < 20, call = it.call ?: false)
            })
        } else if (overBluetooth) {
            rows = Meters.pick((piState?.channels ?: emptyList()).map {
                val age = floor(it.age * 2) / 2
                MeterRow(it.ch, it.freqMHz, it.strength, age, it.hits60, it.approxTime ?: false,
                    frequent = it.activity == "active" && age < 15, rising = (it.rising ?: false) && age < 20, call = it.call ?: false)
            })
        }
        moreChannels = max(0, rows.size - 3)
        extraMeters = rows.drop(3).take(6)
        meters = rows.take(3)
        receiverQuiet = StatusBuilder.receiverQuiet(tracker, settings, now, view)
        settlePushoverWanted()
        bluetooth.settleWanted()
        mutedSeconds = if (now < mutedUntil) ceil(mutedUntil - now).toInt() else 0
        val problem = if (!settings.sound) "sound is switched off" else audio.problem
        soundOK = problem == null
        soundLine = when {
            problem != null -> "No sound: $problem"
            engine.lastSilenceReason.isNotEmpty() -> "Sound ready. Last match was silent: ${engine.lastSilenceReason}"
            else -> "Sound ready, output ${audio.routeName}"
        }
        if (tab == Tab.LOGS) logLines = log.recent
        if (tab == Tab.LOGS || tab == Tab.PI) refreshDiagnostics(now, state)
        val line = widgetLine()
        if (line != lastWidgetLine) { lastWidgetLine = line; MarkWidget.refresh(context, line) }
    }

    private fun refreshDiagnostics(now: Double, state: PiState?) {
        fun s(v: Double) = if (v.isFinite()) "${Fmt.fixed(v, 1)} s" else "-"
        diagnostics = listOf(
            "App version" to version,
            "Server" to linkHost.ifEmpty { "not set" },
            "Connections opened" to "${client.streamOpens}",
            "Messages received" to "$messages",
            "Last message" to s(tracker.sinceRx(now)),
            "Car data age" to s(tracker.carAge(now)),
            "Timing sample" to (tracker.clock?.let { s(now - it.at) } ?: "none"),
            "Burst delay" to (engine.lastDelay?.let { s(it) } ?: "-"),
            "Sound armed" to if (audio.armed) "yes" else "no",
            "Battery" to batteryText,
            "Audio output" to audio.routeName,
            "Other audio playing" to if (audio.otherAudioPlaying) "yes" else "no",
            "Sounds played" to "${audio.soundsPlayed}",
            "Last sound peak" to (audio.lastPeakDB?.let { "${Fmt.fixed(it, 1)} dBFS" } ?: "-"),
            "Times Android stopped the app" to "$suspensions",
            "Last silence reason" to engine.lastSilenceReason.ifEmpty { "-" },
            "Settings" to configStatus,
            "Own rule changes" to if (settings.overrides.isEmpty()) "none" else settings.overrides.sorted().joinToString(", "),
            "Location" to location.statusText,
            "Places recorded" to "${location.track.size}",
            "Session folder (clock may be wrong)" to (state?.session ?: "-"),
            "Background service" to if (serviceRunning) "running" else "stopped",
            "Battery optimisation" to if (ignoringBatteryOptimisation) "off for Slot17" else "on (Android may pause Slot17)",
        )
    }

    val ignoringBatteryOptimisation: Boolean
        get() = (context.getSystemService(Context.POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(context.packageName)

    /** The server has fresh data from the car computer. */
    private fun serverCarFresh(now: Double): Boolean = tracker.frame != null && tracker.carAge(now) <= LinkTracker.CAR_STALE

    /** Bluetooth's word on the car, for the lights and the line: only when the server has nothing fresh from it. */
    private fun bluetoothView(now: Double): BluetoothView? {
        if (demo || serverCarFresh(now) || !bluetooth.feed.heard(now)) return null
        return bluetooth.feed.currentDetector(now)?.let { BluetoothView(it) }
    }

    /** The newest state there is, whichever route brought it, however old: [carStateAge] says how old. */
    val carStateNow: PiState? get() = piState ?: lastMessage?.state

    val carStateAge: Double?
        get() {
            val now = mono()
            if (serverCarFresh(now)) return tracker.carAge(now)
            piState?.let { return it.snapshotAge }
            return if (tracker.frame == null) null else tracker.carAge(now)
        }

    /** The detector runs, by whichever route says so freshly. */
    private val detectorActiveNow: Boolean
        get() {
            val now = mono()
            return if (serverCarFresh(now)) tracker.detectorActive else bluetooth.feed.live(now)
        }

    /**
     * Why this action cannot be used right now, in words, or null if it can. Only a sighting needs the detector
     * running; power off, the Pi log and the check work on a car computer whose detector stopped (1.6.0).
     */
    fun unavailable(feature: String): String? {
        if (demo) return "Not available in demo mode"
        val state = piState ?: return "The Pi is not sending data"
        if (feature == "mark" && !detectorActiveNow) return "The detector is not running, so there is no session to save it in"
        if (feature !in (state.features ?: emptyList())) return "The Pi needs the newer publisher for this"
        if (!serverCarFresh(mono()) && !bluetooth.canSendOps) return "Only over the server, or over Bluetooth from car computer software 1.6.0"
        return null
    }

    val phoneRoundTripMS: Double? get() = tracker.lastRoundTrip?.let { it * 1000 }

    // The ongoing notice (iPhone: the Live Activity) and the widget

    fun liveNotice(): LiveNotice {
        val top = meters.firstOrNull()
        val lastSound = alerts.firstOrNull { it.sound == SoundKind.URGENT || it.sound == SoundKind.CHIME }
        val title = when {
            demo -> "Slot17 demo: fake data"
            top != null && showBars -> "L${top.ch} · ${top.strength?.let { "${Fmt.fixed(it, 1)} dB" } ?: "strength n/a"}" +
                if (Meters.isStrong(top.strength, settings)) " · ${style.strongBadge}" else ""
            else -> headline.title
        }
        val parts = mutableListOf<String>()
        if (top != null && showBars) parts.add(headline.title)
        if (receiverQuiet) parts.add("Receiver hears nothing")
        if (lastSound != null) parts.add("Last ${if (lastSound.sound == SoundKind.URGENT) "urgent beep" else "chime"}: L${lastSound.channel}, " +
            nl.slot17.core.Format.ago((System.currentTimeMillis() - lastSound.atMs) / 1000.0))
        if (parts.isEmpty()) parts.add(headline.detail)
        val important = (lastSound != null && lastSound.id != lastAlertIdShown) || receiverQuiet || headline.level == Level.BAD ||
            Meters.isStrong(top?.strength, settings)
        return LiveNotice(title, parts.joinToString(" · "), mutedSeconds > 0, important)
    }

    private fun updateNotice(now: Double) {
        if (!serviceRunning) return
        val n = liveNotice()
        if (n.copy(important = false) == lastNotice?.copy(important = false)) return
        if (!n.important && now - lastNoticeAt < style.tileGap) return
        lastNotice = n
        lastNoticeAt = now
        lastAlertIdShown = alerts.firstOrNull { it.sound != null }?.id
        if (!settings.liveActivity) return
        context.getSystemService(android.app.NotificationManager::class.java)
            .notify(Notices.LIVE_ID, LiveNotification.build(context, n))
    }

    fun widgetLine(): String {
        val last = sightings.firstOrNull { !it.isRecordingRequest } ?: return headline.title
        return "${last.label.title}: ${last.statusText}"
    }

    // Sound controls

    fun test(kind: SoundKind) {
        if (!audio.armed) audio.arm()
        note("sound test: ${kind.name.lowercase()}")
        audio.play(kind)?.let { note("PROBLEM sound test refused: $it") }
    }

    fun toggleMute() {
        val now = mono()
        mutedUntil = if (now < mutedUntil) Double.NEGATIVE_INFINITY else now + 30
        engine.clearWaiting()
        refresh()
        updateNotice(now)
    }

    private fun applyAudioSettings() {
        audio.volume = settings.volume.toFloat()
        audio.style = settings.beepStyle
    }

    // Sightings

    fun mark(label: Sighting.Label, text: String = "", secondsAgo: Double = 0.0, recording: Boolean = false) {
        val seenAt = System.currentTimeMillis() - (secondsAgo * 1000).toLong()
        if (!recording && Sighting.isRepeat(label, seenAt, sightings)) {
            note("sighting: ${label.wire} again within ${Sighting.REPEAT_WINDOW_S.toInt()} s, counted once")
            return
        }
        val s = Sighting(Sighting.newId(), label, text, seenAt, Sighting.Phase.SENDING, "", recording,
            if (settings.recordLocation && secondsAgo < 30) location.place else null)
        sightings = (listOf(s) + sightings).take(500)
        saveSightings()
        recomputeSummary()
        note("sighting: ${label.wire}${if (recording) " (recording request)" else ""}${s.place?.let { " at " + it.noteText } ?: ""}")
        send(s)
        refresh()
    }

    /** A spoken reply in Android Auto: "police", "nothing"... The reply's own words are not logged. */
    fun carReply(text: String) {
        val label = CarMessages.label(text)
        if (label != null) mark(label, "android auto")
        note("android auto reply: ${label?.wire ?: "not understood"}")
        car.answered(CarMessages.answer(label))
    }

    fun carRead() = car.clear()

    /** A notice, widget or tile button: a normal sighting plus a short sound, because the driver is not looking. */
    fun markFromWidget(label: Sighting.Label, tappedAtMs: Long) {
        mark(label, "widget", max(0.0, (System.currentTimeMillis() - tappedAtMs) / 1000.0))
        audio.play(SoundKind.MARKED)?.let { note("sound for the widget mark not played: $it") }
        if (listening && !asleep) updateNotice(mono())
    }

    private fun send(s: Sighting) {
        if (demo) { update(s.id) { it.copy(phase = Sighting.Phase.SAVED, detail = "Demo only, nothing was sent") }; return }
        val text = listOf(s.note, s.place?.noteText ?: "").filter { it.isNotEmpty() }.joinToString("; ")
        val json = buildJsonObject {
            put("kind", "mark"); put("id", s.id); put("label", s.label.wire); put("note", text.take(500))
            put("age_s", max(0.0, (System.currentTimeMillis() - s.seenAtMs) / 1000.0)); put("phone_utc", s.seenAtMs / 1000.0)
        }
        // Over Bluetooth first (1.6.0): the car computer answers in its next state; the server is the fallback.
        if (sendOverBluetooth(json)) {
            update(s.id) { if (it.phase == Sighting.Phase.SENDING) it.copy(phase = Sighting.Phase.WAITING_FOR_PI, detail = "Sent over Bluetooth") else it }
            return
        }
        if (!hasLink) return
        scope.launch {
            client.send("api/op", json).onSuccess {
                update(s.id) { if (it.phase == Sighting.Phase.SENDING) it.copy(phase = Sighting.Phase.WAITING_FOR_PI, detail = "Waiting for the Pi") else it }
            }.onFailure {
                // Anything but a refusal stays queued; resendPending tries again.
                if (ServerClient.status(it) == 400) update(s.id) { x -> x.copy(phase = Sighting.Phase.FAILED, detail = "The server refused it") }
            }
        }
    }

    /** Keeps trying until the Pi has answered. Repeats are harmless: both ends ignore a known id. */
    private fun resendPending(now: Double) {
        if (now - lastResend < 5 || demo || !(hasLink || bluetooth.canSendOps)) return
        lastResend = now
        // Listed by either route: the server's queue, or the car computer's answers over Bluetooth.
        val listed = (tracker.frame?.message?.ops?.map { it.id } ?: emptyList()).toSet() + bluetooth.feed.answers.map { it.id }
        sightings.filter { it.needsSending && (it.phase == Sighting.Phase.SENDING || it.id !in listed) && it.id !in bluetoothSent }
            .take(50).forEach(::send)
    }

    private fun update(id: String, change: (Sighting) -> Sighting) {
        val i = sightings.indexOfFirst { it.id == id }
        if (i < 0) return
        val changed = change(sightings[i])
        if (changed == sightings[i]) return
        sightings = sightings.toMutableList().also { it[i] = changed }
        saveSightings()
        refresh()
    }

    fun deleteSighting(id: String) {
        sightings = sightings.filter { it.id != id }
        saveSightings()
        recomputeSummary()
    }

    // Operations

    private fun applyOps(ops: List<OpStatus>, command: CommandStatus?) {
        for (op in ops) when (op.kind) {
            "mark" -> update(op.id) { it.apply(op.state, op.detail) }
            "poweroff" -> if (op.id == powerOff.id) powerOff = actionState(op, "Waiting for the Pi…", "Powering off")
            "notify" -> if (op.id == pushoverAction.id) pushoverAction = actionState(op, "Waiting for the car computer…",
                op.detail.ifEmpty { "Saved on the car computer" })
            "bluetooth" -> bluetooth.apply(op)
            "support" -> support.apply(op)
            "doctor" -> if (op.id == doctorAction.id) doctorAction = actionState(op, "The car computer is checking itself…",
                op.detail.ifEmpty { "Checked" })
            // Bluetooth repeats an answer in every state for a few minutes: only one that changes something counts.
            "logs" -> if (op.id == piLogs.id && (piLogs.busy || op.state == "failed")) {
                if (piLogs.busy && op.state == "done") refreshServerLogs()
                piLogs = actionState(op, "Waiting for the Pi…", op.detail.ifEmpty { "Saved on the server" })
            }
            // Over Bluetooth (1.6.0); through the server a stop is a command, handled below.
            "stop" -> if (op.id == stopAction.id) stopAction = actionState(op, "The Pi has it. It should stop within seconds.",
                "Stopped. It starts again when the Pi next powers up.")
        }
        if (stopAction.id == "stop" && stopAction.busy && tracker.detectorState == "stopped") {
            stopAction = ActionState("stop", "Stopped. It starts again when the Pi next powers up.", Level.OK)
        } else if (command != null && stopAction.id == "stop" && stopAction.busy) {
            when (command.state) {
                "pending" -> stopAction = ActionState("stop", "Waiting for the Pi to collect it…", Level.IDLE, true)
                "sent" -> stopAction = ActionState("stop", "The Pi has it. It should stop within seconds.", Level.OK, true)
                "done" -> stopAction = ActionState("stop", "Stopped. It starts again when the Pi next powers up.", Level.OK)
                "expired" -> stopAction = ActionState("stop", "The Pi did not collect it in time, so it was cancelled.", Level.BAD)
            }
        }
    }

    private fun actionState(op: OpStatus, waiting: String, done: String): ActionState = when (op.state) {
        "done" -> ActionState(op.id, done, Level.OK)
        "failed", "expired" -> ActionState(op.id, op.detail.ifEmpty { "It did not work" }, Level.BAD)
        else -> ActionState(op.id, waiting, Level.IDLE, true)
    }

    private fun sendOp(kind: String, fields: JsonObject = JsonObject(emptyMap()), apply: (ActionState) -> Unit) {
        val id = Sighting.newId()
        apply(ActionState(id, "Sending…", Level.IDLE, true))
        val json = JsonObject(fields + mapOf("kind" to JsonPrimitive(kind), "id" to JsonPrimitive(id)))
        if (sendOverBluetooth(json)) {
            apply(ActionState(id, "Sent over Bluetooth…", Level.IDLE, true))
            return
        }
        scope.launch {
            client.send("api/op", json).onFailure { apply(ActionState(id, "Could not reach the server: ${it.message}", Level.BAD)) }
        }
    }

    /** Operations sent over Bluetooth and not yet heard of, with when, for the fallback through the server. */
    private val bluetoothSent = LinkedHashMap<String, Pair<JsonObject, Double>>()

    /**
     * Straight to the car computer when Bluetooth can carry it: no server, no internet needed (1.6.0). The Bluetooth
     * switch has its own way (control frames), and its key only ever comes through the server.
     */
    private fun sendOverBluetooth(json: JsonObject): Boolean {
        val kind = json["kind"]?.jsonPrimitive?.content ?: return false
        val id = json["id"]?.jsonPrimitive?.content ?: return false
        if (demo || kind !in BLUETOOTH_OP_KINDS || !bluetooth.send(json)) return false
        bluetoothSent[id] = json to mono()
        note("sent over Bluetooth: $kind")
        return true
    }

    /** The car computer's answers in its newest state over Bluetooth: applied like the server's. */
    private fun bluetoothAnswers(ops: List<OpStatus>) {
        ops.forEach { bluetoothSent.remove(it.id) }
        applyOps(ops, null)
    }

    /**
     * No word over Bluetooth in time: the same operation through the server too, when there is one; the car computer
     * carries an id out once, whichever route brings it. A Pi log asked for over Bluetooth then goes to the server.
     */
    private fun checkBluetoothSent(now: Double) {
        val late = bluetoothSent.filterValues { now - it.second >= BLUETOOTH_ANSWER_WAIT_S }
        for ((id, sent) in late) {
            bluetoothSent.remove(id)
            if (!hasLink) continue
            val json = JsonObject(sent.first - "to")
            val kind = json["kind"]?.jsonPrimitive?.content
            note("no answer over Bluetooth for $kind: sending it through the server as well")
            scope.launch {
                if (kind == "stop") {
                    stopAction = ActionState("stop", "No answer over Bluetooth: sent through the server", Level.IDLE, true)
                    client.send("api/command", buildJsonObject { put("action", "stop"); put("note", "from the Android app") })
                } else {
                    client.send("api/op", json)
                }
            }
        }
    }

    fun requestPowerOff() {
        expectingSilence = true
        note("power off requested")
        sendOp("poweroff") { powerOff = it }
    }

    fun setPushover(on: Boolean) {
        if (pushoverUnavailable != null) return
        pushoverWanted = on
        note("Pushover messages ${if (on) "on" else "off"} requested")
        sendOp("notify", JsonObject(mapOf("pushover" to JsonPrimitive(on)))) { pushoverAction = it }
    }

    val pushoverOnCar: NotifySwitch? get() = carStateNow?.notify
    val pushoverShown: Boolean get() = pushoverWanted ?: pushoverOnCar?.pushover ?: true

    val pushoverUnavailable: String?
        get() {
            if (demo) return "Not available in demo mode"
            if (!hasLink && !bluetooth.canSendOps) return "Add the private link first"
            val state = carStateNow ?: return "The car computer has not reported yet"
            if ("notify" !in (state.features ?: emptyList())) return "The car computer needs software 1.2.9 or later for this"
            if (state.notify?.configured == false) return "Pushover is not set up on the car computer"
            return null
        }

    private fun settlePushoverWanted() {
        val wanted = pushoverWanted ?: return
        if (pushoverAction.busy) return
        if (pushoverOnCar?.pushover == wanted || pushoverAction.level == Level.BAD) pushoverWanted = null
    }

    /** The Pi screen's setup check: this phone's side, then the car computer's own. */
    val setupRows: List<CheckRow> get() = SetupCheck.phone(hasLink || demo, chain, notices.allowed, ignoringBatteryOptimisation, settings.sound)
    val carRows: List<CheckRow> get() = SetupCheck.car(piState?.doctor)

    fun requestDoctor() { note("setup check requested"); sendOp("doctor") { doctorAction = it } }

    /** Over Bluetooth the log comes straight to this phone (1.6.0) and shows under Logs; otherwise it goes to the server. */
    fun requestPiLogs() {
        note("Pi log requested")
        val fields = if (bluetooth.canReceiveFiles) JsonObject(mapOf("to" to JsonPrimitive("phone"))) else JsonObject(emptyMap())
        sendOp("logs", fields) { piLogs = it }
    }

    fun requestStop() {
        expectingSilence = true
        note("detector stop requested")
        val id = Sighting.newId()
        if (sendOverBluetooth(buildJsonObject { put("kind", "stop"); put("id", id); put("note", "from the Android app, over Bluetooth") })) {
            stopAction = ActionState(id, "Sent over Bluetooth…", Level.IDLE, true)
            return
        }
        stopAction = ActionState("stop", "Sending…", Level.IDLE, true)
        scope.launch {
            client.send("api/command", buildJsonObject { put("action", "stop"); put("note", "from the Android app") })
                .onFailure { stopAction = ActionState("stop", "Could not reach the server: ${it.message}", Level.BAD) }
        }
    }

    // Updates

    val release: ReleaseInfo? get() = lastMessage?.release
    val appUpdate: String? get() = Updates.newer(release?.android, version)
    val piUpdate: String? get() = Updates.newer(release?.software, carStateNow?.software)

    /** Settings > About: download the newer APK and let Android install it over this one. */
    fun installUpdate() {
        val tag = release?.tag ?: return
        if (!updater.allowed) {
            updateState = "Allow Slot17 to install apps, then tap again"
            updater.askPermission()
            return
        }
        updateState = "Downloading..."
        scope.launch {
            runCatching { updater.install(tag) { pct -> scope.launch { updateState = "Downloading... $pct %" } } }
                .onSuccess { note("app update: $tag handed to Android's installer") }
                .onFailure { updateState = "Not downloaded: ${it.message}"; note("app update failed: ${it.message}") }
        }
    }

    private fun announceUpdates() {
        if (demo || notices.permission != "Allowed") return
        val text = Updates.announcement(appUpdate, piUpdate) ?: return
        if (store.prefs.getString("announcedUpdate", null) == text) return
        store.prefs.edit().putString("announcedUpdate", text).apply()
        note("update available: $text")
        notices.postUpdate(text)
    }

    // Logs

    fun uploadAppLog() {
        logUpload = ActionState(text = "Sending…", busy = true)
        note("sending this log to the server")
        val header = diagnostics.joinToString("\n") { "${it.first}: ${it.second}" } + "\n\n"
        val body = header.encodeToByteArray() + log.tail(900_000)
        scope.launch {
            client.uploadLog(body).onSuccess {
                val name = nameFrom(it)
                logUpload = ActionState(text = "Saved as $name", level = Level.OK)
                note("log saved on the server as $name")
                refreshServerLogs()
            }.onFailure { logUpload = ActionState(text = "Could not send: ${it.message}", level = Level.BAD) }
        }
    }

    /**
     * Before a support package: this phone's log and, only when asked, the last day's drive data with places go to
     * the server, which adds the newest of each to the package. Then [then] asks the car computer.
     */
    private fun supportPhoneFiles(phoneLog: Boolean, places: Boolean, then: () -> Unit) {
        scope.launch {
            if (phoneLog) {
                val header = diagnostics.joinToString("\n") { "${it.first}: ${it.second}" } + "\n\n"
                client.uploadLog(header.encodeToByteArray() + log.tail(900_000))
                    .onFailure { note("support package: this phone's log did not reach the server: ${it.message}") }
            }
            if (places) {
                val until = System.currentTimeMillis()
                val from = until - 86_400_000L
                var track = location.track.filter { it.atMs > from }.sortedBy { it.atMs }
                val recent = alerts.filter { it.atMs > from }.sortedBy { it.atMs }.takeLast(2000)
                val marks = sightings.filter { it.seenAtMs > from }
                var body = DriveData.encode(version, from, until, settings, recent, marks, track)
                while (body.size > 950_000 && track.size > 10) {           // the newest places, when a day is too much
                    track = track.takeLast(track.size / 2)
                    body = DriveData.encode(version, from, until, settings, recent, marks, track)
                }
                client.uploadLog(body).onFailure { note("support package: the drive data did not reach the server: ${it.message}") }
            }
            then()
        }
    }

    /** A Pi log that came over Bluetooth (1.6.0), kept on this phone. */
    data class PhoneLog(val name: String, val file: java.io.File, val size: Long, val atMs: Long)

    private val piLogFolder: java.io.File get() = java.io.File(context.filesDir, "pi-logs")

    fun loadPhonePiLogs() {
        phonePiLogs = (piLogFolder.listFiles { f -> f.name.endsWith(".log") } ?: emptyArray())
            .map { PhoneLog(it.name, it, it.length(), it.lastModified()) }.sortedByDescending { it.atMs }
    }

    /** The car computer's log, straight over Bluetooth: saved on this phone, the newest ten kept. */
    private fun bluetoothFile(data: ByteArray) {
        val file = runCatching { ProtocolJson.decodeFromString(BluetoothFile.serializer(), data.toString(Charsets.UTF_8)) }.getOrNull()
        if (file == null || file.kind != "pi-log") {
            note("PROBLEM a file over Bluetooth could not be read")
            return
        }
        val stamp = java.text.SimpleDateFormat("yyyyMMdd-HHmmss", java.util.Locale.UK).format(java.util.Date())
        runCatching {
            piLogFolder.mkdirs()
            val target = java.io.File(piLogFolder, "pi-$stamp-bluetooth.log")
            val tmp = java.io.File(piLogFolder, target.name + ".tmp")
            tmp.writeText(file.text)
            tmp.renameTo(target)
        }.onFailure {
            note("PROBLEM the Pi log from Bluetooth could not be saved: ${it.message}")
            return
        }
        loadPhonePiLogs()
        phonePiLogs.drop(PHONE_PI_LOGS_KEPT).forEach { it.file.delete() }
        loadPhonePiLogs()
        note("Pi log received over Bluetooth (${file.text.length} characters)")
        if (file.op == piLogs.id || piLogs.busy) piLogs = ActionState(piLogs.id, "Received over Bluetooth: see Logs, On server", Level.OK)
    }

    fun readPhonePiLog(log: PhoneLog): String = runCatching { log.file.readText() }.getOrElse { "Could not read it" }

    fun refreshServerLogs() {
        loadPhonePiLogs()
        scope.launch {
            client.listLogs().onSuccess {
                serverLogs = it
                serverLogsNote = if (it.isEmpty()) "Nothing saved on the server yet" else ""
            }.onFailure { serverLogsNote = "Could not list them: ${it.message}" }
        }
    }

    fun fetchServerLog(name: String, done: (String) -> Unit) {
        scope.launch { done(client.fetchLog(name).getOrElse { "Could not load it: ${it.message}" }) }
    }

    fun clearLocalLog() { log.clear(); note("log cleared"); logLines = log.recent }

    fun clearAlertHistory() { alerts = emptyList(); saveHistory(); recomputeSummary() }

    fun selectTab(t: Tab) { tab = t; refresh() }

    fun noticePermissionChanged() { noticePermission = notices.permission }

    private fun note(text: String) = log.write(text)

    private fun nameFrom(reply: ByteArray): String = runCatching {
        ProtocolJson.parseToJsonElement(reply.decodeToString()).jsonObject["name"]?.jsonPrimitive?.content
    }.getOrNull() ?: "on the server"

    // Demo

    /** One channel slowly strengthens, as in the web page's demo. The screen carries a banner. */
    private fun demoMessage(now: Double): ServerMessage {
        val t = (now - demoStart) % 70
        val strength = if (t < 50) 8 + t * 0.55 else 35.5 - (t - 50) * 1.2
        val beat = (t / 4).toInt()
        val matches = if (beat != demoBeat) listOf(MatchRow("demo/${(now * 1000).toLong()}", 154, strength, 0.4)) else emptyList()
        demoBeat = beat
        demoSeq++
        val first = if (screenshotValues) 55.0 else strength
        return ServerMessage("demo", demoSeq, now, 0.0, PiState(
            source = "demo", session = "demo", snapshotAge = 0.0,
            detector = DetectorState("running", windows = 412, hits = 128, unchecked = 6, partial = 3, quietWindows = 0, inputPeak = 1840.0),
            channels = listOf(
                ChannelRow(154, 383.8625, first, 1.0, 14, false, activity = "active"),
                ChannelRow(60, 381.5125, if (screenshotValues) 9.8 else 14.2, 9.0, 3, false),
                ChannelRow(119, 382.9875, null, 31.0, 1, true),
            ),
            matches = if (screenshotValues) emptyList() else matches, features = emptyList(),
            pi = PiHealth(1240.0, 52.4, 1.8, 96.2, 6120.0, 1236.0, 0.0)))
    }

    /** For the screenshot tests: fixed values instead of a moving demo. */
    var screenshotValues = false

    /** For the screenshot tests: run the tick by hand, with the regular one stopped. */
    fun tickNow() = tick()
    fun stopTickLoop() = main.removeCallbacks(tickRunnable)

    companion object {
        /** What may go over Bluetooth (1.6.0); the Bluetooth switch and its key do not. */
        private val BLUETOOTH_OP_KINDS = setOf("mark", "poweroff", "logs", "notify", "doctor", "support", "stop")
        /** Seconds an operation over Bluetooth waits for the car computer's first word before the server gets it too. */
        const val BLUETOOTH_ANSWER_WAIT_S = 8.0
        private const val PHONE_PI_LOGS_KEPT = 10
        private const val CONFIG_CACHE = "remote-config.json"
        private val SOUND_KINDS = listOf(SoundKind.URGENT, SoundKind.CHIME, SoundKind.CALL, SoundKind.WARNING, SoundKind.LINK_LOST)
        const val version = BuildConfig.VERSION_NAME

        fun db(value: Double?): String = value?.let { "${Fmt.fixed(it, 1)} dB" } ?: "no strength"
    }

    private val batteryText: String
        get() {
            val b = context.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
            val percent = b.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
            return "${if (percent in 0..100) "$percent%" else "unknown"}${if (b.isCharging) ", charging" else ""}"
        }
}

/** The drive's records in the shape the iPhone app sends, which the server's drive report reads. */
object DriveData {
    data class Counts(val alerts: Int, val sightings: Int, val places: Int)

    fun encode(app: String, fromMs: Long, untilMs: Long, settings: AlertSettings, alerts: List<AlertRecord>,
               sightings: List<Sighting>, track: List<TrackPoint>): ByteArray {
        fun place(p: nl.slot17.core.Place?) = p?.let {
            buildJsonObject { put("lat", it.lat); put("lon", it.lon); it.speedKmh?.let { v -> put("speedKmh", v) }; it.accuracyM?.let { v -> put("accuracyM", v) } }
        }
        val json = buildJsonObject {
            put("kind", "slot17-drive-data")
            put("app", "$app (Android)")
            put("from", isoTime(fromMs))
            put("until", isoTime(untilMs))
            put("settings", ProtocolJson.encodeToJsonElement(AlertSettings.serializer(), settings))
            put("alerts", JsonArray(alerts.map { a ->
                buildJsonObject {
                    put("id", a.id); put("at", isoTime(a.atMs)); put("channel", a.channel)
                    a.strength?.let { put("strength", it) }; put("age", a.age)
                    a.sound?.let { put("sound", ProtocolJson.encodeToJsonElement(SoundKind.serializer(), it)) }
                    put("reason", a.reason); place(a.place)?.let { put("place", it) }; a.rule?.let { put("rule", it) }
                }
            }))
            put("sightings", JsonArray(sightings.map { s ->
                buildJsonObject {
                    put("id", s.id); put("label", s.label.wire); put("note", s.note); put("seenAt", isoTime(s.seenAtMs))
                    put("phase", ProtocolJson.encodeToJsonElement(Sighting.Phase.serializer(), s.phase))
                    put("detail", s.detail); put("isRecordingRequest", s.isRecordingRequest); place(s.place)?.let { put("place", it) }
                }
            }))
            put("track", JsonArray(track.map { t ->
                buildJsonObject { put("at", isoTime(t.atMs)); put("lat", t.lat); put("lon", t.lon); t.speedKmh?.let { put("speedKmh", it) } }
            }))
        }
        return json.toString().encodeToByteArray()
    }
}
