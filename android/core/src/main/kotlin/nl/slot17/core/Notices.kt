package nl.slot17.core

import kotlinx.serialization.Serializable

/** The newest published Slot17 release, as the display server read it from GitHub. Port of Updates.swift. */
@Serializable
data class ReleaseInfo(
    val software: String,
    /** The iPhone app's version. */
    val app: String? = null,
    val tag: String? = null,
    val url: String? = null,
    /** The Android app's own version (releases from 1.3.1 on; older ones leave it out). */
    val android: String? = null,
)

/** Which updates to mention. Versions compare by their numbers, so 1.10.0 is newer than 1.9.0. */
object Updates {
    fun numbers(version: String): List<Int> =
        version.split(".").map { part -> part.takeWhile { it.isDigit() }.toIntOrNull() ?: 0 }

    fun isNewer(a: String, than: String): Boolean {
        val x = numbers(a)
        val y = numbers(than)
        for (i in 0 until maxOf(x.size, y.size)) {
            val p = x.getOrElse(i) { 0 }
            val q = y.getOrElse(i) { 0 }
            if (p != q) return p > q
        }
        return false
    }

    /** The newer version, or null when there is none or either side is unknown. */
    fun newer(published: String?, running: String?): String? {
        if (published == null || running.isNullOrEmpty() || !isNewer(published, running)) return null
        return published
    }

    /** One line for a notification, or null. */
    fun announcement(app: String?, software: String?): String? {
        val parts = buildList {
            app?.let { add("app $it") }
            software?.let { add("car computer software $it") }
        }
        return if (parts.isEmpty()) null else parts.joinToString(" and ")
    }
}

/**
 * The car computer's faults as notifications from this app, the same faults Pushover reports.
 * Port of FaultNotices.swift: one notice when a fault starts and one when everything is fine again,
 * at most one a minute. No fresh car data is not a fault here: parked, that is normal.
 */
class FaultNotices {
    data class Input(
        /** The detector's state from fresh car data, or null when there is none. */
        val detectorState: String?,
        val reasons: List<String> = emptyList(),
        val receiverQuiet: Boolean = false,
        /** The operator stopped it or powered the Pi off from the app. */
        val expectingSilence: Boolean = false,
    )

    data class Notice(val title: String, val body: String)

    /** The faults already announced in this episode. */
    val announced: Set<String> get() = _announced
    private val _announced = HashSet<String>()
    private var lastSentAt = Double.NEGATIVE_INFINITY

    fun step(input: Input, now: Double): Notice? {
        if (input.detectorState == null || now - lastSentAt < MIN_GAP) return null
        val faults = faults(input)
        val new = faults.filterKeys { it !in _announced }
        if (new.isNotEmpty()) {
            _announced.addAll(new.keys)
            lastSentAt = now
            val keys = new.keys.sorted()
            val words = keys.mapNotNull { new[it] }
            return Notice("Slot17: ${words.firstOrNull() ?: "fault"}",
                if (words.size > 1) words.joinToString(", ") else advice(keys.firstOrNull() ?: ""))
        }
        // Fine again only when running with nothing wrong; "starting" waits.
        if (faults.isEmpty() && _announced.isNotEmpty() && input.detectorState == "running") {
            _announced.clear()
            lastSentAt = now
            return Notice("Slot17: running normally again", "The fault has cleared.")
        }
        // A fault that cleared while another remains is forgotten, so it is announced if it returns.
        if (input.detectorState == "running" || input.detectorState == "degraded") _announced.retainAll(faults.keys)
        return null
    }

    companion object {
        const val MIN_GAP = 60.0

        fun faults(input: Input): Map<String, String> {
            val out = LinkedHashMap<String, String>()
            when (input.detectorState) {
                "degraded" -> {
                    val reasons = input.reasons.filter { it.isNotEmpty() }
                    if (reasons.isEmpty()) out["degraded"] = "Detector degraded"
                    for (reason in reasons) out["degraded: $reason"] = reason
                }
                "stopped" -> if (!input.expectingSilence) out["stopped"] = "Detector stopped"
            }
            if (input.receiverQuiet) out["quiet"] = "The receiver hears nothing"
            return out
        }

        private fun advice(key: String): String = when (key) {
            "quiet" -> "Check the antenna cable. Input level is not calibrated, so this is a hint, not a proof."
            "stopped" -> "It starts again only when the Pi next powers up."
            else -> "See the Pi screen in the app for details."
        }
    }
}
