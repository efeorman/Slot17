package nl.slot17.core

import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.Transient
import kotlinx.serialization.json.Json

/** Unknown fields are ignored and optional ones may be missing, so an older Pi publisher keeps working. */
val ProtocolJson = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
}

/** What the display server sends, as it sends it. Port of ios/Slot17/Core/Protocol.swift. */
@Serializable
data class ServerMessage(
    val boot: String,
    val seq: Int,
    @SerialName("server_mono_s") val serverMono: Double,
    @SerialName("server_age_s") val serverAge: Double? = null,
    val state: PiState? = null,
    val command: CommandStatus? = null,
    val ops: List<OpStatus>? = null,
    /** The newest published release (server 1.2.9 and later). */
    val release: ReleaseInfo? = null,
) {
    companion object {
        fun decode(text: String): ServerMessage? =
            runCatching { ProtocolJson.decodeFromString(serializer(), text) }.getOrNull()
    }
}

@Serializable
data class PiState(
    val source: String? = null,
    val session: String? = null,
    @SerialName("snapshot_age_s") val snapshotAge: Double,
    val detector: DetectorState,
    val channels: List<ChannelRow>,
    val matches: List<MatchRow>,
    val features: List<String>? = null,
    val pi: PiHealth? = null,
    val update: UpdateState? = null,
    val link: PiLink? = null,
    /** Whether the car computer sends Pushover messages (1.2.9 and later). */
    val notify: NotifySwitch? = null,
    /** The detector's software version, for example "1.2.9" (1.2.9 and later). */
    val software: String? = null,
    /** The car computer's own setup check (1.3.1 and later); the server keeps the last one between pushes. */
    val doctor: DoctorReport? = null,
    /** The car computer's Bluetooth link to the apps (1.4.0 and later). */
    val bluetooth: BluetoothState? = null,
)

@Serializable
data class DetectorState(
    val state: String,
    @SerialName("heartbeat_unchanged_s") val heartbeatUnchanged: Double? = null,
    val reasons: List<String>? = null,
    val windows: Int? = null,
    val hits: Int? = null,
    val unchecked: Int? = null,
    val partial: Int? = null,
    @SerialName("quiet_windows") val quietWindows: Int? = null,
    @SerialName("input_peak_counts") val inputPeak: Double? = null,
    /** How many quiet windows in a row the Pi itself counts as "receiver hears nothing". */
    @SerialName("quiet_alarm_windows") val quietAlarmWindows: Int? = null,
    /** The band's quiet level in the detector's own dB (not dBm). Lower is better. */
    @SerialName("floor_db") val floorDB: Double? = null,
    /** Windows so far in which 60 or more channels fired at once: interference, not radios. */
    @SerialName("whole_band_windows") val wholeBandWindows: Int? = null,
)

@Serializable
data class ChannelRow(
    val ch: Int,
    @SerialName("freq_mhz") val freqMHz: Double,
    @SerialName("strength_db") val strength: Double? = null,
    @SerialName("age_s") val age: Double,
    @SerialName("hits_60s") val hits60: Int,
    @SerialName("approx_time") val approxTime: Boolean? = null,
    /** "active" or "intermittent": burst density, not decoded content. */
    val activity: String? = null,
    /** The Pi's id for the latest event on this channel. */
    @SerialName("id") val burstID: String? = null,
    /** Getting louder; louder is not nearer. */
    val rising: Boolean? = null,
    /** A call pattern on the channel in the last 10 s. Traffic, not identity. */
    val call: Boolean? = null,
)

@Serializable
data class MatchRow(
    val id: String,
    val ch: Int,
    @SerialName("strength_db") val strength: Double? = null,
    @SerialName("age_s") val age: Double,
    val call: Boolean? = null,
    val rising: Boolean? = null,
    /** How the training word was recognised: "standard", "weak" or "coherent". Never who. */
    val rule: String? = null,
    /** The key the car computer gave this match over Bluetooth; never from or to the server (see MatchKey). */
    @Transient val key: String? = null,
)

/** What the car computer's reports travel over. Every field optional. */
@Serializable
data class PiLink(
    val via: String? = null,
    val networks: List<String>? = null,
    val device: String? = null,
    val pushes: Double? = null,
    val failed: Double? = null,
    val failovers: Double? = null,
    @SerialName("median_ms") val medianMS: Double? = null,
    @SerialName("slowest_ms") val slowestMS: Double? = null,
    @SerialName("wifi_dbm") val wifiDBm: Double? = null,
    @SerialName("clock_synced") val clockSynced: Boolean? = null,
    val stick: Stick? = null,
) {
    @Serializable
    data class Stick(
        val connected: Boolean? = null,
        val network: String? = null,
        val provider: String? = null,
        val roaming: Boolean? = null,
        val bars: Double? = null,
        val rsrp: Double? = null,
        val rsrq: Double? = null,
        val snr: Double? = null,
        val cell: String? = null,
        @SerialName("ping_ms") val pingMS: Double? = null,
        @SerialName("ping_worst_ms") val pingWorstMS: Double? = null,
        @SerialName("ping_lost") val pingLost: Double? = null,
        @SerialName("down_s") val downS: Double? = null,
    )

    /** The network in use, in words. */
    val viaText: String
        get() {
            val name = when (via) {
                "4G stick" -> "the 4G stick"
                "Wi-Fi" -> "Wi-Fi (hotspot)"
                "cable" -> "a network cable"
                "none" -> "no network"
                else -> via ?: "unknown"
            }
            return device?.let { "$name ($it)" } ?: name
        }
}

/** Plain words for a 4G signal. The usual LTE ranges, not a measurement of this car. */
object SignalWords {
    fun rsrp(dBm: Double): String = when {
        dBm >= -90 -> "good"
        dBm >= -105 -> "fair"
        dBm >= -115 -> "weak"
        else -> "very weak"
    }

    fun wifi(dBm: Double): String = when {
        dBm >= -60 -> "good"
        dBm >= -70 -> "fair"
        else -> "weak"
    }
}

/** The car computer's Pushover switch. `configured` is false where Pushover was never set up. */
@Serializable
data class NotifySwitch(val pushover: Boolean, val configured: Boolean? = null)

/**
 * The car computer's Bluetooth link, as its publisher reports it (1.4.0 and later): the switch, the name phones
 * show, and how the link is doing. `keyId` names the key the frames are signed with; the key itself never
 * travels in a state.
 */
@Serializable
data class BluetoothState(
    val on: Boolean,
    val state: String = "off",
    val name: String? = null,
    @SerialName("key_id") val keyId: String? = null,
    val subscribed: Boolean = false,
    val frames: Long? = null,
    val matches: Long? = null,
    val mtu: Int? = null,
    @SerialName("since_s") val since: Double? = null,
    val error: String? = null,
)

@Serializable
data class PiHealth(
    @SerialName("uptime_s") val uptime: Double? = null,
    @SerialName("temp_c") val temperature: Double? = null,
    @SerialName("load_1m") val load: Double? = null,
    @SerialName("disk_free_gb") val diskFreeGB: Double? = null,
    @SerialName("mem_free_mb") val memFreeMB: Double? = null,
    @SerialName("session_s") val sessionSeconds: Double? = null,
    @SerialName("dropped_samples") val droppedSamples: Double? = null,
)

/** "current", "installed", "staged", "rejected", "error" or "off". */
@Serializable
data class UpdateState(
    val state: String,
    val commit: String? = null,
    val seq: Int? = null,
    val detail: String? = null,
)

@Serializable
data class CommandStatus(
    val action: String? = null,
    val state: String,
    @SerialName("age_s") val age: Double? = null,
)

@Serializable
data class OpStatus(val id: String, val kind: String, val state: String, val detail: String)

@Serializable
data class ServerLogEntry(
    val name: String,
    val size: Int,
    @SerialName("age_s") val ageSeconds: Double,
)

/** Turns the server's event stream into one JSON text per message. */
class SseParser {
    private var pending = ByteArray(0)

    fun feed(data: ByteArray): List<String> {
        pending += data
        if (pending.size > 2_000_000) pending = ByteArray(0)
        val messages = mutableListOf<String>()
        while (true) {
            val end = indexOfBlankLine(pending)
            if (end < 0) break
            val block = pending.copyOfRange(0, end)
            pending = pending.copyOfRange(end + 2, pending.size)
            val text = strictUtf8(block) ?: continue
            val lines = text.split("\n")
                .filter { it.startsWith("data:") }
                .map { it.substring(5).trimStart(' ') }
            if (lines.isNotEmpty()) messages.add(lines.joinToString("\n"))
        }
        return messages
    }

    private fun indexOfBlankLine(bytes: ByteArray): Int {
        for (i in 0 until bytes.size - 1) if (bytes[i] == 10.toByte() && bytes[i + 1] == 10.toByte()) return i
        return -1
    }

    /** Like Swift's String(data:encoding:.utf8): invalid UTF-8 gives null, never replacement characters. */
    private fun strictUtf8(bytes: ByteArray): String? = try {
        Charsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(bytes)).toString()
    } catch (e: CharacterCodingException) {
        null
    }
}
