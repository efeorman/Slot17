package nl.slot17.core

import kotlin.math.ceil
import kotlin.math.max
import kotlin.math.min

/** Port of ios/Slot17/Core/Status.swift: plain-language state, the same words as the web page. */
enum class Level { OK, WARN, BAD, IDLE }

data class HeadlineStatus(val level: Level, val title: String, val detail: String)

/** One link in the chain from this phone to the receiver. */
data class ChainItem(val id: String, val name: String, var level: Level, var detail: String)

data class MeterRow(
    val ch: Int,
    val freqMHz: Double,
    val strength: Double?,
    val age: Double,
    val hits60: Int,
    val approxTime: Boolean,
    /** Many bursts close together: energy density only, not "speech" or "data". */
    val frequent: Boolean = false,
    /** Getting louder over the last minute; louder is not nearer. */
    val rising: Boolean = false,
    /** A call pattern on this channel in the last 10 s. */
    val call: Boolean = false,
)

/** What a meter's colour means: what the app would do, never how near or what vehicle. */
enum class MeterColour { WEAK, YELLOW, RED, GREY }

object Meters {
    const val SEGMENTS = 10
    /** The strength at which the bar starts (the detector's 6 dB candidate threshold). */
    const val FLOOR_DB = 6.0
    const val PULSE_EVERY = 10.0
    const val PULSE_FRESH_AGE = 2.0

    /** Full only at the urgent level; every detection lights at least one segment. */
    fun level(strength: Double?, urgent: Double = 50.0): Int {
        val s = strength ?: return 0
        if (s >= urgent) return SEGMENTS
        val span = max(1.0, urgent - FLOOR_DB)
        val lit = ceil((s - FLOOR_DB) / span * SEGMENTS).toInt()
        return min(SEGMENTS - 1, max(1, lit))
    }

    /** Colour by alert eligibility. Quiet mode's higher chime level counts. */
    fun colour(strength: Double?, settings: AlertSettings): MeterColour {
        val s = strength ?: return MeterColour.GREY
        if (s >= settings.effectiveRed) return MeterColour.RED
        return if (s >= settings.yellow) MeterColour.YELLOW else MeterColour.WEAK
    }

    fun isStrong(strength: Double?, settings: AlertSettings): Boolean =
        strength != null && strength >= settings.urgentLevel

    /** Recent channels first (under 15 s), then strongest. Older than a minute is dropped. */
    fun pick(rows: List<MeterRow>): List<MeterRow> = rows.filter { it.age <= 60 }.sortedWith { a, b ->
        val ra = a.age < 15
        val rb = b.age < 15
        when {
            ra != rb -> if (ra) -1 else 1
            (a.strength ?: -1e9) != (b.strength ?: -1e9) -> (b.strength ?: -1e9).compareTo(a.strength ?: -1e9)
            else -> a.age.compareTo(b.age)
        }
    }

    fun opacity(age: Double): Double = max(0.35, 1 - max(0.0, age - 10) / 50 * 0.65)
}

/** How often whole-band interference came in about the last minute, from the Pi's running counts. */
class InterferenceMeter {
    private data class Sample(val at: Double, val windows: Int, val wide: Int)
    private val samples = ArrayList<Sample>()

    fun add(now: Double, windows: Int, wide: Int) {
        samples.lastOrNull()?.let { if (windows < it.windows) samples.clear() }  // a new session began
        samples.add(Sample(now, windows, wide))
        while (samples.size > 2 && now - samples[1].at >= 60) samples.removeAt(0)
    }

    /** Percent of the windows in about the last minute, or null until enough of them have passed. */
    val percent: Double?
        get() {
            val first = samples.firstOrNull() ?: return null
            val last = samples.last()
            if (last.windows - first.windows < 20) return null
            return 100.0 * max(0, last.wide - first.wide) / (last.windows - first.windows)
        }
}

object Format {
    fun ago(s: Double): String = when {
        !s.isFinite() -> "never"
        s < 1 -> "now"
        s < 90 -> "${Fmt.roundAway(s).toInt()} s ago"
        s < 5400 -> "${Fmt.roundAway(s / 60).toInt()} min ago"
        else -> "${Fmt.roundAway(s / 3600).toInt()} h ago"
    }

    /** "85.2 dB, 3.5 above the receiver alone": every dB above is a dB of reach lost to noise. */
    fun floor(floor: Double, quiet: Double?): String =
        if (quiet == null) "${Fmt.fixed(floor, 1)} dB"
        else "${Fmt.fixed(floor, 1)} dB, ${Fmt.fixed(max(0.0, floor - quiet), 1)} above the receiver alone"

    /** The car computer's software, in a few words for the Pi screen. */
    fun software(update: UpdateState): String {
        val commit = (update.commit ?: "").take(7)
        val version = commit.ifEmpty { "as installed by hand" }
        val detail = update.detail ?: ""
        return when (update.state) {
            "installed" -> "$version, updated at this start"
            "staged" -> "$version; an update installs at the next start"
            "rejected" -> "$version; an update failed its checks" + if (detail.isEmpty()) "" else ": $detail"
            "error" -> "$version; update problem" + if (detail.isEmpty()) "" else ": $detail"
            else -> "$version, up to date when last checked"
        }
    }

    fun duration(s: Double): String {
        val total = s.toInt()
        val h = total / 3600
        val m = total % 3600 / 60
        return when {
            h > 0 -> "$h h $m min"
            m > 0 -> "$m min"
            else -> "$total s"
        }
    }
}

object StatusBuilder {
    const val QUIET_ALARM_WINDOWS = 40

    fun headline(t: LinkTracker, now: Double, lastDelay: Double?, hasCredentials: Boolean): HeadlineStatus {
        if (!hasCredentials) return HeadlineStatus(Level.IDLE, "Not set up", "Add your private link in Settings")
        if (t.authFailed) return HeadlineStatus(Level.BAD, "Link not accepted", "Check your private link in Settings")
        val f = t.frame ?: return HeadlineStatus(Level.IDLE, "Connecting…", "Looking for the server")
        val rx = t.sinceRx(now)
        if (rx > LinkTracker.LINK_LOST) {
            return HeadlineStatus(Level.IDLE, "Connection lost", "No update for ${rx.toInt()} s, reconnecting")
        }
        val st = f.message.state
        if (f.message.serverAge == null || st == null) {
            return HeadlineStatus(Level.IDLE, "Waiting for the car", "The server has heard nothing from the Pi yet")
        }
        if (!t.clockReady(now)) return HeadlineStatus(Level.IDLE, "Checking timing", "Waiting for a fresh timing sample")
        val carAge = t.carAge(now)
        if (carAge > LinkTracker.CAR_STALE) {
            return HeadlineStatus(Level.IDLE, "No data from the car", "Last update ${Format.ago(carAge)}. Hotspot or Pi offline")
        }
        val d = st.detector
        return when (d.state) {
            "running" -> {
                val detail = lastDelay?.let { "Delay up to ${Fmt.fixed(ceil(it * 10) / 10, 1)} s after the burst" }
                    ?: "${d.windows ?: 0} windows analysed"
                if (t.demo) HeadlineStatus(Level.OK, "Demo running", "Fake data, nothing is being detected")
                else HeadlineStatus(Level.OK, "Detector running", detail)
            }
            "starting" -> HeadlineStatus(Level.IDLE, "Detector starting", "Waiting for the first analysis")
            "degraded" -> HeadlineStatus(Level.WARN, "Detector degraded",
                (d.reasons ?: emptyList()).joinToString(", ").ifEmpty { "See status on the Pi" })
            "stale" -> HeadlineStatus(Level.BAD, "Detector not responding",
                "Heartbeat unchanged for ${(d.heartbeatUnchanged ?: 0.0).toInt()} s")
            "stopped" -> HeadlineStatus(Level.IDLE, "Detector stopped", "It starts again when the Pi next powers up")
            else -> HeadlineStatus(Level.IDLE, "Unknown state", d.state)
        }
    }

    fun receiverQuiet(t: LinkTracker, settings: AlertSettings, now: Double): Boolean {
        if (!t.detectorActive || t.demo || !settings.antennaWarning) return false
        // Old data says nothing about the receiver now; losing the link has its own warning.
        if (t.carAge(now) > LinkTracker.CAR_STALE) return false
        val detector = t.frame?.message?.state?.detector ?: return false
        return (detector.quietWindows ?: 0) >= quietLimit(detector, settings)
    }

    /** The Pi's own limit when it sends one; the setting is for older detectors, whose windows were 1.5 s. */
    fun quietLimit(detector: DetectorState?, settings: AlertSettings): Int {
        val own = detector?.quietAlarmWindows
        return if (own != null && own > 0) own else settings.receiverQuietWindows
    }

    fun chain(t: LinkTracker, now: Double, hasCredentials: Boolean, quietWindows: Int = QUIET_ALARM_WINDOWS): List<ChainItem> {
        val server = ChainItem("server", "Server", Level.BAD, "Not reachable")
        val pi = ChainItem("pi", "Pi", Level.IDLE, "Unknown")
        val detector = ChainItem("detector", "Detector", Level.IDLE, "Unknown")
        val receiver = ChainItem("receiver", "Receiver", Level.IDLE, "Unknown")
        if (!hasCredentials) { server.level = Level.IDLE; server.detail = "Not set up" }
        if (t.authFailed) server.detail = "Link refused"
        val f = t.frame
        if (f != null && !t.authFailed && t.sinceRx(now) <= LinkTracker.LINK_LOST) {
            server.level = Level.OK
            server.detail = "Connected"
            val carAge = t.carAge(now)
            val d = f.message.state?.detector
            when {
                f.message.serverAge == null || f.message.state == null -> { pi.level = Level.BAD; pi.detail = "Never heard" }
                !t.clockReady(now) -> pi.detail = "Checking"
                carAge > LinkTracker.CAR_STALE -> { pi.level = Level.BAD; pi.detail = "Silent ${Format.ago(carAge)}" }
                d != null -> {
                    pi.level = Level.OK; pi.detail = "Connected"
                    when (d.state) {
                        "running" -> { detector.level = Level.OK; detector.detail = "Running" }
                        "degraded" -> { detector.level = Level.WARN; detector.detail = "Degraded" }
                        "starting" -> detector.detail = "Starting"
                        "stale" -> { detector.level = Level.BAD; detector.detail = "Not responding" }
                        "stopped" -> detector.detail = "Stopped"
                        else -> detector.detail = d.state
                    }
                    if (t.detectorActive) {
                        if ((d.quietWindows ?: 0) >= quietWindows) {
                            receiver.level = Level.BAD; receiver.detail = "Hears nothing"
                        } else {
                            receiver.level = Level.OK; receiver.detail = "No fault"
                        }
                    } else {
                        receiver.detail = "Closed"
                    }
                }
            }
        }
        return listOf(server, pi, detector, receiver)
    }
}
