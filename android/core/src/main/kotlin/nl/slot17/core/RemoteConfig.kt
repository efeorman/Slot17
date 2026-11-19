package nl.slot17.core

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.exp
import kotlin.math.max
import kotlin.math.pow
import kotlin.math.sin
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** How one sound is made: notes on a waveform with a simple envelope, or a WAV file from the server. */
@Serializable
data class SoundSpec(
    val wave: String = "sine",  // sine, square, triangle or bell
    val peak: Double = 0.6,
    @SerialName("attack_s") val attackS: Double = 0.01,
    @SerialName("release_s") val releaseS: Double = 0.1,
    @SerialName("fade_s") val fadeS: Double? = null,  // bell only: how fast it rings out
    val notes: List<List<Double>> = emptyList(),       // [frequency Hz, start s, length s]
    val wav: String? = null,                           // a file served from /api/sounds/ instead
) {
    /** Null if usable, otherwise the reason. A bad sound must never reach the speaker. */
    val problem: String?
        get() {
            wav?.let { return if (WAV_NAME.matches(it)) null else "bad file name" }
            if (wave !in setOf("sine", "square", "triangle", "bell")) return "unknown wave $wave"
            if (peak !in 0.05..1.0) return "peak out of range"
            if (attackS !in 0.0..0.5 || releaseS !in 0.0..3.0) return "envelope out of range"
            fadeS?.let { if (it !in 0.01..5.0) return "fade out of range" }
            if (notes.size !in 1..16) return "needs 1 to 16 notes"
            for (note in notes) {
                if (note.size != 3 || note[0] !in 50.0..10_000.0 || note[1] !in 0.0..3.0 || note[2] !in 0.01..3.0) {
                    return "a note is out of range"
                }
            }
            return if (totalSeconds > 4) "longer than 4 s" else null
        }

    val totalSeconds: Double get() = notes.maxOfOrNull { it[1] + it[2] } ?: 0.0

    companion object {
        private val WAV_NAME = Regex("^[a-z0-9_-]{1,40}\\.wav$")
    }
}

/** The settings file on the display server. Every field optional; missing ones keep defaults. */
@Serializable
data class RemoteConfig(
    val schema: Int? = null,
    val message: String? = null,
    val rules: Rules? = null,
    val warnings: Warnings? = null,
    val marks: Marks? = null,
    val location: Location? = null,
    val display: Display? = null,
    val sounds: Map<String, SoundSpec>? = null,
) {
    @Serializable
    data class Rules(
        @SerialName("red_db") val redDB: Double? = null,
        @SerialName("yellow_db") val yellowDB: Double? = null,
        @SerialName("urgent_db") val urgentDB: Double? = null,
        @SerialName("urgent_gap_s") val urgentGapS: Double? = null,
        @SerialName("chime_gap_any_s") val chimeGapAnyS: Double? = null,
        @SerialName("chime_gap_channel_s") val chimeGapChannelS: Double? = null,
        @SerialName("max_age_s") val maxAgeS: Double? = null,
        @SerialName("quiet_red_db") val quietRedDB: Double? = null,
        @SerialName("quiet_gap_s") val quietGapS: Double? = null,
        @SerialName("call_db") val callDB: Double? = null,
        @SerialName("call_gap_channel_s") val callGapChannelS: Double? = null,
        @SerialName("call_gap_any_s") val callGapAnyS: Double? = null,
    )

    @Serializable
    data class Warnings(
        @SerialName("link_lost_after_s") val linkLostAfterS: Double? = null,
        @SerialName("receiver_quiet_windows") val receiverQuietWindows: Int? = null,
        @SerialName("receiver_tone_repeat_s") val receiverToneRepeatS: Double? = null,
    )

    @Serializable
    data class Marks(@SerialName("summary_window_s") val summaryWindowS: Double? = null)

    @Serializable
    data class Location(@SerialName("track_every_s") val trackEveryS: Double? = null)

    /** Meter colours ("#RRGGBB") and words. */
    @Serializable
    data class Display(
        @SerialName("weak_colour") val weakColour: String? = null,
        @SerialName("silent_colour") val silentColour: String? = null,
        @SerialName("chime_colour") val chimeColour: String? = null,
        @SerialName("strong_outline_colour") val strongOutlineColour: String? = null,
        @SerialName("strong_badge") val strongBadge: String? = null,
        @SerialName("frequent_text") val frequentText: String? = null,
        @SerialName("pulse_on_new_strong") val pulseOnNewStrong: Boolean? = null,
        @SerialName("tile_gap_s") val tileGapS: Double? = null,
        @SerialName("floor_quiet_db") val floorQuietDB: Double? = null,
    ) {
        val problems: List<String>
            get() {
                val out = mutableListOf<String>()
                val colours = listOf("weak_colour" to weakColour, "silent_colour" to silentColour,
                    "chime_colour" to chimeColour, "strong_outline_colour" to strongOutlineColour)
                for ((name, value) in colours) {
                    if (value == null) continue
                    val ratio = HexColour.contrast(value, HexColour.BACKGROUND)
                    if (ratio == null) { out.add("$name is not #RRGGBB"); continue }
                    if (ratio < 3) out.add("$name is too dark to read on the background")
                    if (HexColour.isGreenish(value)) out.add("$name is green, which reads as safe")
                }
                if (silentColour != null && chimeColour != null && silentColour.lowercase() == chimeColour.lowercase()) {
                    out.add("silent_colour and chime_colour are the same")
                }
                strongBadge?.let { if (it.trim().isEmpty() || it.length > 10) out.add("strong_badge must be 1-10 characters") }
                frequentText?.let { if (it.length > 20) out.add("frequent_text longer than 20 characters") }
                tileGapS?.let { if (it !in 1.0..60.0) out.add("tile_gap_s out of range") }
                floorQuietDB?.let { if (it !in -300.0..300.0) out.add("floor_quiet_db out of range") }
                return out
            }
    }

    /** Every reason this file cannot be used. Empty means it can. */
    val problems: List<String>
        get() {
            val out = mutableListOf<String>()
            fun check(value: Double?, range: ClosedFloatingPointRange<Double>, name: String) {
                if (value != null && value !in range) out.add("$name out of range")
            }
            schema?.let { if (it != 1) out.add("unknown schema $it") }
            message?.let { if (it.length > 200) out.add("message longer than 200 characters") }
            rules?.let { r ->
                check(r.redDB, 5.0..80.0, "red_db"); check(r.yellowDB, 0.0..80.0, "yellow_db")
                check(r.urgentDB, 10.0..90.0, "urgent_db"); check(r.urgentGapS, 0.0..60.0, "urgent_gap_s")
                check(r.chimeGapAnyS, 0.0..600.0, "chime_gap_any_s")
                check(r.chimeGapChannelS, 0.0..600.0, "chime_gap_channel_s"); check(r.maxAgeS, 2.0..60.0, "max_age_s")
                check(r.quietRedDB, 5.0..90.0, "quiet_red_db"); check(r.quietGapS, 0.0..600.0, "quiet_gap_s")
                // 99 switches the call tone off from the server.
                check(r.callDB, 5.0..99.0, "call_db"); check(r.callGapChannelS, 0.0..3600.0, "call_gap_channel_s")
                check(r.callGapAnyS, 0.0..600.0, "call_gap_any_s")
                if (r.redDB != null && r.yellowDB != null && r.yellowDB > r.redDB) out.add("yellow_db above red_db")
            }
            warnings?.let { w ->
                check(w.linkLostAfterS, 5.0..600.0, "link_lost_after_s")
                check(w.receiverToneRepeatS, 30.0..7200.0, "receiver_tone_repeat_s")
                w.receiverQuietWindows?.let { if (it !in 3..1000) out.add("receiver_quiet_windows out of range") }
            }
            check(marks?.summaryWindowS, 10.0..600.0, "summary_window_s")
            check(location?.trackEveryS, 2.0..600.0, "track_every_s")
            out += display?.problems ?: emptyList()
            for ((name, spec) in sounds ?: emptyMap()) {
                if (SoundKind.fromConfigName(name) == null) out.add("unknown sound $name")
                spec.problem?.let { out.add("sound $name: $it") }
            }
            return out
        }

    @Serializable
    private data class Envelope(val version: String, val config: RemoteConfig)

    companion object {
        /** The server's `/api/config` answer: `{"version": ..., "config": {...}}`, or null. */
        fun decode(text: String): Pair<String, RemoteConfig>? = runCatching {
            val e = ProtocolJson.decodeFromString(Envelope.serializer(), text)
            e.version to e.config
        }.getOrNull()
    }
}

/** Colour checks for the server's meter colours. Port of DisplayStyle.swift's HexColour. */
object HexColour {
    /** The app's background, which every meter colour must stand out against. */
    const val BACKGROUND = "#0B0E13"

    fun rgb(hex: String): Triple<Double, Double, Double>? {
        if (hex.length != 7 || hex[0] != '#') return null
        val digits = hex.substring(1)
        if (!digits.all { it in '0'..'9' || it in 'a'..'f' || it in 'A'..'F' }) return null
        val value = digits.toLong(16)
        return Triple(((value shr 16) and 0xFF) / 255.0, ((value shr 8) and 0xFF) / 255.0, (value and 0xFF) / 255.0)
    }

    /** WCAG contrast ratio between two colours, 1..21. */
    fun contrast(a: String, b: String): Double? {
        val x = rgb(a) ?: return null
        val y = rgb(b) ?: return null
        fun ch(v: Double) = if (v <= 0.03928) v / 12.92 else ((v + 0.055) / 1.055).pow(2.4)
        fun lum(c: Triple<Double, Double, Double>) = 0.2126 * ch(c.first) + 0.7152 * ch(c.second) + 0.0722 * ch(c.third)
        val l1 = lum(x)
        val l2 = lum(y)
        return (max(l1, l2) + 0.05) / (minOf(l1, l2) + 0.05)
    }

    /** Roughly green: the hue people read as "safe", which a signal meter must not suggest. */
    fun isGreenish(hex: String): Boolean {
        val (r, g, b) = rgb(hex) ?: return false
        return g > r * 1.25 && g > b * 1.15 && g > 0.35
    }
}

/** Turns a sound description into samples. Pure, so tests can check every sound without audio. */
object SoundSynth {
    fun render(spec: SoundSpec, rate: Double = 44_100.0): FloatArray {
        val frames = ((spec.totalSeconds + 0.02) * rate).toInt()
        if (frames <= 0) return FloatArray(0)
        val out = FloatArray(frames)
        for (note in spec.notes) {
            val frequency = note[0]
            val start = note[1]
            val length = note[2]
            val begin = (start * rate).toInt()
            val count = minOf((length * rate).toInt(), frames - begin)
            if (count <= 0) continue
            for (i in 0 until count) {
                val t = i / rate
                var level = 1.0
                if (spec.attackS > 0 && t < spec.attackS) level = t / spec.attackS
                val remaining = length - t
                if (spec.releaseS > 0 && remaining < spec.releaseS) level = minOf(level, max(0.0, remaining / spec.releaseS))
                out[begin + i] += (level * wave(spec, frequency, t)).toFloat()
            }
        }
        val top = out.maxOfOrNull { abs(it) } ?: 0f
        if (top <= 0f) return out
        val gain = spec.peak.toFloat() / top
        for (i in out.indices) out[i] *= gain
        return out
    }

    private fun wave(spec: SoundSpec, frequency: Double, t: Double): Double {
        val phase = (t * frequency) % 1.0
        return when (spec.wave) {
            "square" -> if (phase < 0.5) 1.0 else -1.0
            "triangle" -> 4 * abs(phase - 0.5) - 1
            "bell" -> {
                val fade = spec.fadeS ?: 0.45
                val partials = listOf(Triple(1.0, 1.0, 1.0), Triple(2.0, 0.28, 0.49), Triple(3.0, 0.1, 0.31),
                    Triple(4.07, 0.05, 0.2))
                partials.sumOf { (mult, amp, decay) -> amp * exp(-t / (fade * decay)) * sin(2 * PI * frequency * mult * t) }
            }
            else -> sin(2 * PI * frequency * t)
        }
    }
}
