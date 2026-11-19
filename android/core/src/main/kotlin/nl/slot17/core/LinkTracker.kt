package nl.slot17.core

import kotlin.math.max

/** One accepted message, with what is needed to age it later. */
class Frame(val message: ServerMessage, var ageFloor: Double, var acceptedAt: Double)

/**
 * Ordering and timing of server messages. Port of ios/Slot17/Core/LinkTracker.swift (itself the web
 * page's accept/elapsed/live logic): ages are upper bounds, so a slow clock sample may suppress a
 * sound but never freshen data. Times are seconds on a monotonic clock that keeps counting in sleep
 * (on Android, SystemClock.elapsedRealtime), passed in so the rules stay testable.
 */
class LinkTracker {
    data class ClockSync(val boot: String, val upper: Double, val at: Double)
    enum class Acceptance { REFUSED, REPEATED, FRESH, FRESH_AND_NEW_SCOPE }

    var frame: Frame? = null
        private set
    var rxAt = 0.0
        private set
    var clock: ClockSync? = null
        private set
    /** The phone's last timed request to the server, there and back, in seconds. */
    var lastRoundTrip: Double? = null
        private set
    var scope: String? = null
        private set
    var authFailed = false
    var demo = false
    private var boot: String? = null
    private var lastSeq = -1
    private var lastStamp = Double.NEGATIVE_INFINITY
    private val retired = HashSet<String>()

    fun reset() {
        frame = null; rxAt = 0.0; clock = null; scope = null; boot = null
        lastSeq = -1; lastStamp = Double.NEGATIVE_INFINITY; retired.clear(); authFailed = false
    }

    fun accept(m: ServerMessage, now: Double): Acceptance {
        var newScope = false
        if (m.boot != boot) {
            if (m.boot in retired) return Acceptance.REFUSED
            if (boot != null && clock?.boot != m.boot && !demo) return Acceptance.REFUSED
            boot?.let { retired.add(it) }
            boot = m.boot; lastSeq = -1; lastStamp = Double.NEGATIVE_INFINITY
            newScope = true
        }
        if (m.seq < lastSeq || m.serverMono <= lastStamp) return Acceptance.REFUSED
        val fresh = m.seq > lastSeq
        val thisScope = "${m.state?.source ?: ""}/${m.state?.session ?: ""}"
        if (scope != thisScope) { scope = thisScope; newScope = true }
        val current = frame
        val floor = if (!fresh && current != null && clockReady(now)) elapsed(current, now) else 0.0
        lastStamp = m.serverMono
        lastSeq = m.seq
        frame = Frame(m, floor, now)
        rxAt = now
        authFailed = false
        if (!fresh) return Acceptance.REPEATED
        return if (newScope) Acceptance.FRESH_AND_NEW_SCOPE else Acceptance.FRESH
    }

    /** A timed request gives an upper bound on the server-to-phone clock offset. */
    fun applyProbe(m: ServerMessage, begin: Double, end: Double): Acceptance {
        if (m.boot in retired || end - begin > 4) return Acceptance.REFUSED
        val current = frame
        val floor = if (current != null && clockReady(end)) elapsed(current, end) else 0.0
        clock = ClockSync(m.boot, m.serverMono - begin, end)
        lastRoundTrip = end - begin
        if (current != null && current.message.boot == m.boot) {
            current.ageFloor = floor
            current.acceptedAt = end
        }
        return accept(m, end)
    }

    fun clockReady(now: Double): Boolean {
        if (demo) return true
        val c = clock ?: return false
        if (c.boot != boot) return false
        return now - c.at <= CLOCK_TTL
    }

    fun elapsed(f: Frame, now: Double): Double {
        if (!clockReady(now)) return Double.POSITIVE_INFINITY
        val m = f.message
        val c = clock
        val transit = when {
            demo -> now - m.serverMono
            c != null -> max(0.0, now + c.upper - m.serverMono) + 0.03 + max(0.0, now - c.at) * 0.001
            else -> return Double.POSITIVE_INFINITY
        }
        return max((m.serverAge ?: 0.0) + transit, f.ageFloor + (now - f.acceptedAt))
    }

    fun sinceRx(now: Double): Double = if (frame == null) Double.POSITIVE_INFINITY else now - rxAt

    fun carAge(now: Double): Double {
        val f = frame ?: return Double.POSITIVE_INFINITY
        return (f.message.state?.snapshotAge ?: 0.0) + elapsed(f, now)
    }

    val detectorState: String? get() = frame?.message?.state?.detector?.state
    val detectorActive: Boolean get() = detectorState == "running" || detectorState == "degraded"

    fun live(now: Double): Boolean =
        !authFailed && clockReady(now) && frame != null && sinceRx(now) <= LINK_LOST &&
            carAge(now) <= CAR_STALE && detectorActive

    /** Matches stay waiting only for the very first clock sample. Anything else discards them. */
    fun mayHoldMatches(now: Double): Boolean =
        clock == null && !authFailed && sinceRx(now) <= LINK_LOST && detectorActive

    companion object {
        const val LINK_LOST = 6.0
        const val CAR_STALE = 8.0
        const val CLOCK_TTL = 30.0
        const val CLOCK_REFRESH = 10.0
        const val REOPEN_AFTER = 9.0
        const val CLEAR_AFTER = 20.0
    }
}
