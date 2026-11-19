package nl.slot17.core

import kotlin.math.max
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Port of ios/Slot17/Core/AlertEngine.swift. Same defaults, same names, same rules. */
@Serializable
data class AlertSettings(
    val red: Double = 22.0,
    val yellow: Double = 12.0,
    val sound: Boolean = true,
    val urgent: Boolean = true,
    val repeatNeeded: Boolean = false,
    val antennaWarning: Boolean = true,
    val linkLostTone: Boolean = true,
    val volume: Double = 1.0,
    val beepStyle: BeepStyle = BeepStyle.MIX,
    val keepScreenOn: Boolean = true,
    val autoSleepMinutes: Int = 30,
    // Rules the server's settings file can change without an app update.
    val urgentLevel: Double = 50.0,
    val urgentGap: Double = 1.0,
    val anyGap: Double = 6.0,
    val channelGap: Double = 30.0,
    val maxAge: Double = 10.0,
    val quiet: Boolean = false,
    val quietRed: Double = 28.0,
    val quietGap: Double = 30.0,
    /** The soft call tone: a call pattern from this strength up to the chime level. */
    val callLevel: Double = 18.0,
    val callGap: Double = 60.0,     // per channel
    val callAnyGap: Double = 15.0,  // between call tones; never holds back a chime or an urgent beep
    val linkLostAfter: Double = 30.0,
    val receiverQuietWindows: Int = 40,
    val receiverToneRepeat: Double = 300.0,
    val summaryWindow: Double = 120.0,
    val trackEvery: Double = 10.0,
    val recordLocation: Boolean = true,
    /** The ongoing notification (iOS: the Live Activity). */
    val liveActivity: Boolean = true,
    /** The car computer's faults as notifications from this app (only while the app runs). */
    val faultNotices: Boolean = false,
    /** Settings the operator changed in the app; the server's file leaves these alone. */
    val overrides: Set<String> = emptySet(),
) {
    /** Quiet mode raises the chime level and spaces chimes out. Urgent beeps are never changed by it. */
    val effectiveRed: Double get() = if (quiet) max(red, quietRed) else red
    val effectiveAnyGap: Double get() = if (quiet) max(anyGap, quietGap) else anyGap
    val effectiveChannelGap: Double get() = if (quiet) max(channelGap, quietGap) else channelGap

    /** Back to the app's own rules, again except for anything the operator set in the app. */
    fun restoreBuiltInRules(): AlertSettings {
        val d = AlertSettings()
        fun <T> pick(key: String, mine: T, builtIn: T): T = if (key in overrides) mine else builtIn
        val s = copy(
            red = pick("red", red, d.red), yellow = pick("yellow", yellow, d.yellow),
            urgentLevel = pick("urgentLevel", urgentLevel, d.urgentLevel),
            urgentGap = pick("urgentGap", urgentGap, d.urgentGap), anyGap = pick("anyGap", anyGap, d.anyGap),
            channelGap = pick("channelGap", channelGap, d.channelGap), maxAge = pick("maxAge", maxAge, d.maxAge),
            quietRed = pick("quietRed", quietRed, d.quietRed), quietGap = pick("quietGap", quietGap, d.quietGap),
            callLevel = pick("callLevel", callLevel, d.callLevel), callGap = pick("callGap", callGap, d.callGap),
            callAnyGap = pick("callAnyGap", callAnyGap, d.callAnyGap),
            linkLostAfter = pick("linkLostAfter", linkLostAfter, d.linkLostAfter),
            receiverQuietWindows = pick("receiverQuietWindows", receiverQuietWindows, d.receiverQuietWindows),
            receiverToneRepeat = pick("receiverToneRepeat", receiverToneRepeat, d.receiverToneRepeat),
            summaryWindow = pick("summaryWindow", summaryWindow, d.summaryWindow),
            trackEvery = pick("trackEvery", trackEvery, d.trackEvery),
        )
        return if (s.yellow > s.red) s.copy(yellow = s.red) else s
    }

    /** Takes the server's values, except for anything the operator set in the app. */
    fun apply(config: RemoteConfig): AlertSettings {
        fun <T> take(key: String, mine: T, server: T?): T = if (server != null && key !in overrides) server else mine
        val r = config.rules
        val w = config.warnings
        val s = copy(
            red = take("red", red, r?.redDB), yellow = take("yellow", yellow, r?.yellowDB),
            urgentLevel = take("urgentLevel", urgentLevel, r?.urgentDB),
            urgentGap = take("urgentGap", urgentGap, r?.urgentGapS),
            anyGap = take("anyGap", anyGap, r?.chimeGapAnyS),
            channelGap = take("channelGap", channelGap, r?.chimeGapChannelS),
            maxAge = take("maxAge", maxAge, r?.maxAgeS),
            quietRed = take("quietRed", quietRed, r?.quietRedDB), quietGap = take("quietGap", quietGap, r?.quietGapS),
            callLevel = take("callLevel", callLevel, r?.callDB), callGap = take("callGap", callGap, r?.callGapChannelS),
            callAnyGap = take("callAnyGap", callAnyGap, r?.callGapAnyS),
            linkLostAfter = take("linkLostAfter", linkLostAfter, w?.linkLostAfterS),
            receiverQuietWindows = take("receiverQuietWindows", receiverQuietWindows, w?.receiverQuietWindows),
            receiverToneRepeat = take("receiverToneRepeat", receiverToneRepeat, w?.receiverToneRepeatS),
            summaryWindow = take("summaryWindow", summaryWindow, config.marks?.summaryWindowS),
            trackEvery = take("trackEvery", trackEvery, config.location?.trackEveryS),
        )
        return if (s.yellow > s.red) s.copy(yellow = s.red) else s
    }

    companion object {
        /** The names of settings that differ, used to remember what the operator changed by hand. */
        fun changedKeys(a: AlertSettings, b: AlertSettings): Set<String> = buildSet {
            if (a.red != b.red) add("red")
            if (a.yellow != b.yellow) add("yellow")
            if (a.quietRed != b.quietRed) add("quietRed")
            if (a.quietGap != b.quietGap) add("quietGap")
        }
    }
}

@Serializable
enum class BeepStyle(val title: String) {
    @SerialName("Over music") MIX("Over music"),
    @SerialName("Lower music") DUCK("Lower music"),
    @SerialName("Voice prompt") PROMPT("Voice prompt"),
}

@Serializable
enum class SoundKind {
    /** CALL: soft, for a radio in a call below the chime level. */
    @SerialName("urgent") URGENT,
    @SerialName("chime") CHIME,
    @SerialName("call") CALL,
    @SerialName("warning") WARNING,
    @SerialName("linkLost") LINK_LOST,
    /** A short blip: a widget tap was recorded. Not an alert. */
    @SerialName("marked") MARKED;

    /** The sounds the app had before remote settings existed; used until the server says otherwise. */
    val builtIn: SoundSpec
        get() = when (this) {
            URGENT -> SoundSpec("square", 0.7, 0.005, 0.015,
                notes = listOf(listOf(2000.0, 0.0, 0.135), listOf(2000.0, 0.17, 0.135), listOf(2000.0, 0.34, 0.135)))
            CHIME -> SoundSpec("triangle", 0.65, 0.015, 0.25,
                notes = listOf(listOf(988.0, 0.0, 0.45), listOf(1319.0, 0.17, 0.45)))
            // Softer and lower than the chime: two sine notes, under half its level.
            CALL -> SoundSpec("sine", 0.3, 0.04, 0.35, notes = listOf(listOf(523.0, 0.0, 0.45), listOf(659.0, 0.22, 0.45)))
            WARNING -> SoundSpec("sine", 0.5, 0.03, 0.52, notes = listOf(listOf(392.0, 0.0, 0.55), listOf(294.0, 0.26, 0.55)))
            LINK_LOST -> SoundSpec("sine", 0.5, 0.03, 0.47,
                notes = listOf(listOf(523.0, 0.0, 0.5), listOf(392.0, 0.22, 0.5), listOf(294.0, 0.44, 0.5)))
            MARKED -> SoundSpec("sine", 0.5, 0.005, 0.05, notes = listOf(listOf(1175.0, 0.0, 0.08), listOf(1568.0, 0.09, 0.12)))
        }

    companion object {
        fun fromConfigName(name: String): SoundKind? = when (name) {
            "urgent" -> URGENT
            "chime" -> CHIME
            "call" -> CALL
            "warning" -> WARNING
            "link_lost" -> LINK_LOST
            else -> null
        }
    }
}

/** What happened with one recognised burst, and why. */
data class AlertDecision(
    val id: String,
    val channel: Int,
    val strength: Double?,
    val age: Double,
    val outcome: Outcome,
    val rule: String? = null,
) {
    sealed interface Outcome {
        data class Played(val kind: SoundKind) : Outcome
        data class Silent(val reason: String) : Outcome
    }
}

/**
 * The web page's sound rule, moved into the app so it can run with the screen locked.
 *
 * Strength alone decides, and it is not a distance or a vehicle test. A new match of at least the
 * urgent level plays the urgent beep with a short gap. A new red match chimes at most once per
 * channel gap and once per overall gap. A sound that cannot play now is dropped, never queued.
 */
class AlertEngine {
    class Waiting(val match: MatchRow, val frame: Frame?, val channels: List<ChannelRow>, val scope: String?)

    /** Everything the rule depends on at this moment. */
    class Context(
        val now: Double,
        val live: Boolean,
        val mayHold: Boolean,
        val scope: String?,
        val settings: AlertSettings,
        val armed: Boolean,
        val audioProblem: String?,
        val mutedUntil: Double,
        val age: (Waiting) -> Double,
        /** Plays the sound; returns null when it played, otherwise why it could not. */
        val play: (SoundKind) -> String?,
    )

    var lastSilenceReason = ""
        private set
    var lastDelay: Double? = null
        private set
    private val waiting = ArrayDeque<Waiting>()
    private val seen = HashSet<String>()
    private val seenOrder = ArrayDeque<String>()
    private val lastBeep = HashMap<Int, Double>()
    private var lastAny = Double.NEGATIVE_INFINITY
    private var lastUrgent = Double.NEGATIVE_INFINITY
    private val lastCall = HashMap<Int, Double>()
    private var lastCallAny = Double.NEGATIVE_INFINITY

    val waitingCount: Int get() = waiting.size

    fun resetContext() {
        waiting.clear()
        lastDelay = null
    }

    fun clearWaiting() = waiting.clear()

    fun ingest(matches: List<MatchRow>, frame: Frame?, channels: List<ChannelRow>, scope: String?) {
        for (match in matches) {
            if (match.id in seen) continue
            remember(match.id)
            waiting.addLast(Waiting(match, frame, channels, scope))
            if (waiting.size > WAITING_LIMIT) waiting.removeFirst()
        }
    }

    private fun remember(id: String) {
        seen.add(id)
        seenOrder.addLast(id)
        if (seenOrder.size > SEEN_LIMIT) seen.remove(seenOrder.removeFirst())
    }

    private fun isUrgent(strength: Double?, s: AlertSettings): Boolean =
        s.urgent && strength != null && strength >= s.urgentLevel

    fun flush(c: Context): List<AlertDecision> {
        if (!c.live) {
            if (!c.mayHold) waiting.clear()
            return emptyList()
        }
        // Urgent first: an ordinary chime in the same push must not use up the 1 s gap. Stable sort.
        val batch = waiting.withIndex().sortedWith(compareBy({ !isUrgent(it.value.match.strength, c.settings) }, { it.index }))
            .map { it.value }
        waiting.clear()
        return batch.map { decide(it, c) }
    }

    private fun decide(p: Waiting, c: Context): AlertDecision {
        val s = c.settings
        val age = c.age(p)
        lastDelay = age
        fun finish(outcome: AlertDecision.Outcome): AlertDecision {
            lastSilenceReason = (outcome as? AlertDecision.Outcome.Silent)?.reason ?: ""
            return AlertDecision(p.match.id, p.match.ch, p.match.strength, age, outcome, p.match.rule)
        }
        fun silent(why: String) = finish(AlertDecision.Outcome.Silent(why))

        if (p.scope != c.scope) return silent("the link or the detector was not live")
        if (s.repeatNeeded) {
            val row = p.channels.firstOrNull { it.ch == p.match.ch }
            if ((row?.hits60 ?: 0) < 2) return silent("“repeat needed” is on and this channel matched once")
        }
        val db = p.match.strength ?: return silent("the match had no usable strength")
        val urgent = isUrgent(db, s)
        // A radio in a call below the chime level gets the soft call tone instead of silence.
        val callTier = !urgent && p.match.call == true && db >= s.callLevel && db < s.effectiveRed
        // The red level and Quiet mode govern chimes only; an urgent beep is never held back by them.
        if (!urgent && !callTier && db < s.effectiveRed) {
            return silent("${Fmt.fixed(db, 1)} dB is below the ${Fmt.fixed(s.effectiveRed, 0)} dB red level" +
                if (s.quiet) " (Quiet mode)" else "")
        }
        if (age > s.maxAge) return silent("it reached the phone ${Fmt.fixed(age, 1)} s old (limit ${Fmt.fixed(s.maxAge, 0)} s)")
        c.audioProblem?.let { return silent(it) }
        if (c.now < c.mutedUntil) return silent("muted by the speaker button")
        if (!s.sound) return silent("sound is switched off")
        if (!c.armed) return silent("sound is not armed")

        val kind: SoundKind
        if (urgent) {
            // Only another urgent beep holds this back (8 November: a weak chime 14 ms earlier silenced 51 dB).
            if (c.now - lastUrgent < s.urgentGap) {
                return silent("another urgent beep played under ${Fmt.fixed(s.urgentGap, 0)} s ago")
            }
            kind = SoundKind.URGENT
        } else if (callTier) {
            if (c.now - (lastCall[p.match.ch] ?: Double.NEGATIVE_INFINITY) < s.callGap) {
                return silent("this channel's call tone played under ${Fmt.fixed(s.callGap, 0)} s ago")
            }
            if (c.now - lastCallAny < s.callAnyGap) {
                return silent("another call tone played under ${Fmt.fixed(s.callAnyGap, 0)} s ago")
            }
            if (c.now - lastAny < 1) return silent("another sound played under 1 s ago")
            c.play(SoundKind.CALL)?.let { return silent(it) }
            lastCall[p.match.ch] = c.now
            lastCallAny = c.now
            return finish(AlertDecision.Outcome.Played(SoundKind.CALL))
        } else {
            if (c.now - (lastBeep[p.match.ch] ?: Double.NEGATIVE_INFINITY) < s.effectiveChannelGap) {
                return silent("this channel chimed under ${Fmt.fixed(s.effectiveChannelGap, 0)} s ago")
            }
            if (c.now - lastAny < s.effectiveAnyGap) {
                return silent("another chime played under ${Fmt.fixed(s.effectiveAnyGap, 0)} s ago")
            }
            kind = SoundKind.CHIME
        }
        c.play(kind)?.let { return silent(it) }
        lastBeep[p.match.ch] = c.now
        lastAny = c.now
        if (urgent) lastUrgent = c.now
        return finish(AlertDecision.Outcome.Played(kind))
    }

    companion object {
        private const val SEEN_LIMIT = 400
        private const val WAITING_LIMIT = 20
    }
}
