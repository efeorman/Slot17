package nl.slot17.core

import java.net.URI
import java.util.UUID
import kotlin.math.abs
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** The private link the operator pastes in, split into server address and key. Port of Links.swift. */
data class ServerLink(val base: String, val key: String) {
    companion object {
        /** Accepts `http(s)://host:port/?key=...`, with or without a trailing slash or spaces. */
        fun parse(text: String): ServerLink? {
            val uri = runCatching { URI(text.trim()) }.getOrNull() ?: return null
            val scheme = uri.scheme?.lowercase() ?: return null
            if (scheme != "http" && scheme != "https") return null
            val host = uri.host?.takeIf { it.isNotEmpty() } ?: return null
            val key = (uri.query ?: "").split("&")
                .map { it.split("=", limit = 2) }
                .firstOrNull { it[0] == "key" }
                ?.getOrNull(1) ?: return null
            if (key.length < 8 || !key.all { it.code < 128 && (it.isLetterOrDigit() || it == '-' || it == '_') }) return null
            val port = if (uri.port >= 0) ":${uri.port}" else ""
            return ServerLink("$scheme://$host$port", key)
        }
    }
}

/** Where the phone was. Kept on the phone and sent with sightings, for later analysis only. */
@Serializable
data class Place(val lat: Double, val lon: Double, val speedKmh: Double? = null, val accuracyM: Double? = null) {
    val noteText: String
        get() = buildList {
            add("lat=${Fmt.fixed(lat, 6)}")
            add("lon=${Fmt.fixed(lon, 6)}")
            speedKmh?.let { add("kmh=${Fmt.fixed(it, 0)}") }
            accuracyM?.let { add("acc_m=${Fmt.fixed(it, 0)}") }
        }.joinToString(";")
}

/** A sighting the operator reported. The phone keeps it until the Pi has confirmed it. */
@Serializable
data class Sighting(
    val id: String,
    val label: Label,
    val note: String,
    /** Wall-clock time, milliseconds since 1970. */
    val seenAtMs: Long,
    val phase: Phase,
    val detail: String,
    val isRecordingRequest: Boolean,
    val place: Place? = null,
) {
    @Serializable
    enum class Label(val title: String) {
        @SerialName("police") POLICE("Police"),
        @SerialName("ambulance") AMBULANCE("Ambulance"),
        @SerialName("fire") FIRE("Fire"),
        @SerialName("unknown") UNKNOWN("Other"),
        // "noise" is the detector's own word for "nothing seen"; p0/mark.py accepts it.
        @SerialName("noise") NOTHING("Saw nothing");

        val wire: String get() = when (this) { NOTHING -> "noise"; else -> name.lowercase() }
        /** An emergency vehicle was actually seen. */
        val isVehicle: Boolean get() = this == POLICE || this == AMBULANCE || this == FIRE

        companion object {
            /** The vehicle buttons, without "Saw nothing", which has its own wider button. */
            val vehicles = listOf(POLICE, AMBULANCE, FIRE, UNKNOWN)
        }
    }

    @Serializable
    enum class Phase { @SerialName("sending") SENDING, @SerialName("waitingForPi") WAITING_FOR_PI,
        @SerialName("saved") SAVED, @SerialName("failed") FAILED }

    /** The server may forget a sighting if it restarts, and the Pi ignores a repeat by id. */
    val needsSending: Boolean get() = phase == Phase.SENDING || phase == Phase.WAITING_FOR_PI

    val statusText: String
        get() = when (phase) {
            Phase.SENDING -> "Sending…"
            Phase.WAITING_FOR_PI -> detail.ifEmpty { "Waiting for the Pi" }
            Phase.SAVED, Phase.FAILED -> detail
        }

    /** Saved stays saved: from 1.6.0 a sighting can go by both routes, and the server's copy may expire later. */
    fun apply(state: String, newDetail: String): Sighting = if (phase == Phase.SAVED && state != "done") this else when (state) {
        "pending", "sent" -> copy(phase = Phase.WAITING_FOR_PI, detail = if (state == "sent") "The Pi has it" else "Waiting for the Pi")
        "done" -> copy(phase = Phase.SAVED, detail = newDetail.ifEmpty { "Saved on the Pi" })
        "failed", "expired" -> copy(phase = Phase.FAILED, detail = newDetail.ifEmpty { "Not saved" })
        else -> this
    }

    companion object {
        fun newId(): String = UUID.randomUUID().toString().replace("-", "").lowercase()

        /** The same button again within this long is the same sighting (11 November: a double tap saved one car twice). */
        const val REPEAT_WINDOW_S = 3.0

        fun isRepeat(label: Label, seenAtMs: Long, among: List<Sighting>): Boolean = among.any {
            it.label == label && !it.isRecordingRequest && abs(it.seenAtMs - seenAtMs) < REPEAT_WINDOW_S * 1000
        }
    }
}

@Serializable
data class AlertRecord(
    val id: String,
    val atMs: Long,
    val channel: Int,
    val strength: Double? = null,
    val age: Double,
    val sound: SoundKind? = null,
    val reason: String,
    val place: Place? = null,
    /** Which recognition rule matched (car computer 1.2.9 and later). */
    val rule: String? = null,
)

/** What the operator's marks say about the sounds: the honest answer to "are the beeps useful?" */
data class MarkSummary(
    val urgent: Row = Row(),
    val chime: Row = Row(),
    /** Vehicles marked with no sound at all in the window before: possible misses. */
    val vehiclesWithoutSound: Int = 0,
    val vehiclesMarked: Int = 0,
) {
    data class Row(val played: Int = 0, val vehicleSeen: Int = 0, val nothingSeen: Int = 0) {
        val unmarked: Int get() = played - vehicleSeen - nothingSeen
    }

    companion object {
        /** Each sound counts towards the first mark made within `windowS` seconds after it. */
        fun make(alerts: List<AlertRecord>, sightings: List<Sighting>, windowS: Double): MarkSummary {
            val windowMs = windowS * 1000
            val marks = sightings.filter { !it.isRecordingRequest }.sortedBy { it.seenAtMs }
            var urgent = Row()
            var chime = Row()
            for (alert in alerts) {
                val kind = alert.sound
                if (kind != SoundKind.URGENT && kind != SoundKind.CHIME) continue
                val next = marks.firstOrNull { it.seenAtMs >= alert.atMs && it.seenAtMs - alert.atMs <= windowMs }
                var row = if (kind == SoundKind.URGENT) urgent else chime
                row = row.copy(played = row.played + 1)
                if (next != null) {
                    if (next.label.isVehicle) row = row.copy(vehicleSeen = row.vehicleSeen + 1)
                    if (next.label == Sighting.Label.NOTHING) row = row.copy(nothingSeen = row.nothingSeen + 1)
                }
                if (kind == SoundKind.URGENT) urgent = row else chime = row
            }
            var marked = 0
            var without = 0
            for (mark in marks) {
                if (!mark.label.isVehicle) continue
                marked++
                val heard = alerts.any { it.sound != null && it.atMs <= mark.seenAtMs && mark.seenAtMs - it.atMs <= windowMs }
                if (!heard) without++
            }
            return MarkSummary(urgent, chime, without, marked)
        }
    }
}
