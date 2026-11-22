package nl.slot17.core

import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/** The support package's words for the screens, the same on both phones (ios/Slot17/Core/Support.swift). */
object SupportWords {
    /** The three choices, in the order the app shows them. */
    val choices = listOf("quick" to "Quick check", "drive" to "One drive", "everything" to "Everything from a drive")

    fun choice(key: String): String = choices.firstOrNull { it.first == key }?.second ?: key

    /** "21 Nov 17:47, 72 min, 37 matches, 2 faults" in this phone's time zone. */
    fun drive(d: DriveRow, zone: ZoneId = ZoneId.systemDefault()): String {
        val start = d.start?.let {
            runCatching {
                OffsetDateTime.parse(it).atZoneSameInstant(zone).format(DateTimeFormatter.ofPattern("d MMM HH:mm", Locale.ENGLISH))
            }.getOrNull()
        } ?: "start unknown"
        val parts = mutableListOf(start)
        d.minutes?.let { parts.add("${it.toInt()} min") }
        parts.add("${d.matches ?: 0} matches")
        if ((d.faults ?: 0) > 0) parts.add("${d.faults} fault${if (d.faults == 1) "" else "s"}")
        return parts.joinToString(", ")
    }

    fun size(bytes: Long): String = when {
        bytes >= 1_000_000 -> String.format(Locale.ENGLISH, "%.1f MB", bytes / 1e6)
        else -> "${(bytes + 999) / 1000} KB"
    }

    /** What the car computer is doing with a package, or null when nothing is on its way. */
    fun progress(p: SupportProgress?): String? = when (p?.state) {
        "building" -> "The car computer is making the package…"
        "uploading" -> {
            val total = p.total ?: 0
            if (total > 0) "Uploading ${size(p.sent ?: 0)} of ${size(total)}…" else "Uploading…"
        }
        else -> null
    }

    /** A window in minutes after the drive's start, from two fields; null for the whole drive, or why not. */
    fun window(from: String, to: String): Result<List<Int>?> {
        if (from.isBlank() && to.isBlank()) return Result.success(null)
        val a = from.trim().toIntOrNull()
        val b = to.trim().toIntOrNull()
        if (a == null || b == null || a < 0 || b > 1440 || a >= b) {
            return Result.failure(IllegalArgumentException("Give two minutes after the start, the first smaller, up to 1440"))
        }
        return Result.success(listOf(a, b))
    }
}
