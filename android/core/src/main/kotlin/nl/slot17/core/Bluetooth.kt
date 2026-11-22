package nl.slot17.core

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import kotlin.math.max
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * The car computer's Bluetooth link, version 1: the frames `p0/bluetooth_frames.py` sends, decoded and
 * checked. `shared/parity/ble-frames.json` holds frames that this, the iPhone app and the car computer
 * must all read the same way. docs/design/BLUETOOTH.md describes the whole link.
 */
object BluetoothFormat {
    const val SERVICE_UUID = "f9cb0001-aa93-4349-a576-0e70a736266f"
    const val FRAMES_UUID = "f9cb0002-aa93-4349-a576-0e70a736266f"
    const val INFO_UUID = "f9cb0003-aa93-4349-a576-0e70a736266f"
    const val CONTROL_UUID = "f9cb0004-aa93-4349-a576-0e70a736266f"
    const val VERSION = 1
    const val HEADER_BYTES = 10
    const val TAG_BYTES = 8
    const val MAX_FRAME = 180
    const val KEY_BYTES = 32
    private const val STATUS_BYTES = 20
    private const val MATCH_BYTES = 15
    private const val CHANNEL_BYTES = 17
    private const val I16_NONE: Short = Short.MIN_VALUE
    private val DETECTOR_STATES = listOf("stopped", "starting", "running", "degraded", "stale")
    private val RULES = listOf(null, "standard", "weak", "coherent")
    private val ACTIVITIES = listOf(null, "active", "intermittent")
    private const val CONTROL_KIND = 16

    /** What a phone can ask over the link itself. Switching on needs the server: an off link hears nothing. */
    enum class Action(val code: Int) { OFF(1), RENAME(2) }

    class FrameError(message: String) : Exception(message)

    /** A checked frame. Matches carry the key every route shares, so a match plays once. */
    sealed class Frame {
        abstract val boot: Long
        abstract val counter: Long

        data class Status(override val boot: Long, override val counter: Long, val detector: DetectorState) : Frame()
        data class Matches(override val boot: Long, override val counter: Long, val matches: List<MatchRow>) : Frame()
        data class Channels(override val boot: Long, override val counter: Long, val channels: List<ChannelRow>) : Frame()
        data class Reply(override val boot: Long, override val counter: Long, val action: Action?, val ok: Boolean, val text: String) : Frame()
    }

    /**
     * A control frame for the car computer: [boot] and [counter] are those of the newest frame from it, and each
     * counter works once, so a recording of the command cannot be played back later.
     */
    fun control(key: ByteArray, boot: Long, counter: Long, action: Action, name: String = ""): ByteArray {
        val words = if (action == Action.RENAME) name.toByteArray(Charsets.US_ASCII) else ByteArray(0)
        val head = ByteBuffer.allocate(HEADER_BYTES + 2 + words.size).order(ByteOrder.LITTLE_ENDIAN)
            .put(VERSION.toByte()).put(CONTROL_KIND.toByte()).putInt(boot.toInt()).putInt(counter.toInt())
            .put(action.code.toByte()).put(words.size.toByte()).put(words).array()
        return head + hmac(key, head).copyOf(TAG_BYTES)
    }

    /** Decodes [frame] after checking its tag with [key]. Throws [FrameError]. */
    fun decode(frame: ByteArray, key: ByteArray): Frame {
        if (frame.size < HEADER_BYTES + TAG_BYTES || frame.size > MAX_FRAME) throw FrameError("wrong length")
        val head = frame.copyOfRange(0, frame.size - TAG_BYTES)
        val tag = frame.copyOfRange(frame.size - TAG_BYTES, frame.size)
        if (!MessageDigest.isEqual(tag, hmac(key, head).copyOf(TAG_BYTES))) throw FrameError("not signed with this key")
        val b = ByteBuffer.wrap(head).order(ByteOrder.LITTLE_ENDIAN)
        val version = b.get().toInt() and 0xFF
        if (version != VERSION) throw FrameError("version $version")
        val kind = b.get().toInt() and 0xFF
        val boot = b.int.toLong() and 0xFFFFFFFFL
        val counter = b.int.toLong() and 0xFFFFFFFFL
        val body = head.size - HEADER_BYTES
        return when (kind) {
            1 -> {
                if (body != STATUS_BYTES) throw FrameError("status length")
                val state = b.get().toInt() and 0xFF
                b.get()
                val windows = b.int.toLong() and 0xFFFFFFFFL
                val hits = b.int.toLong() and 0xFFFFFFFFL
                val quiet = b.u16()
                val alarm = b.u16()
                val whole = b.u16()
                val unchanged = b.u16()
                val floor = b.short
                Frame.Status(boot, counter, DetectorState(
                    state = DETECTOR_STATES.getOrNull(state) ?: "unknown",
                    heartbeatUnchanged = unchanged / 10.0,
                    windows = windows.coerceAtMost(Int.MAX_VALUE.toLong()).toInt(),
                    hits = hits.coerceAtMost(Int.MAX_VALUE.toLong()).toInt(),
                    quietWindows = quiet, quietAlarmWindows = alarm.takeIf { it > 0 },
                    floorDB = tenths(floor), wholeBandWindows = whole,
                ))
            }
            2 -> Frame.Matches(boot, counter, rows(b, body, MATCH_BYTES) {
                val k = ByteArray(8).also { get(it) }.hex()
                val ch = u16()
                val strength = tenths(short)
                val age = u16() / 1000.0
                val rule = get().toInt() and 0xFF
                MatchRow(id = "bt:$k", ch = ch, strength = strength, age = age, rule = RULES.getOrNull(rule), key = k)
            })
            3 -> Frame.Channels(boot, counter, rows(b, body, CHANNEL_BYTES) {
                val ch = u16()
                val freq = (int.toLong() and 0xFFFFFFFFL) / 1e4
                val strength = tenths(short)
                val age = u16() / 10.0
                val hits = u16()
                val flags = get().toInt() and 0xFF
                val burst = ByteArray(4).also { get(it) }.hex()
                ChannelRow(ch = ch, freqMHz = freq, strength = strength, age = age, hits60 = hits,
                           approxTime = flags and 4 != 0, activity = ACTIVITIES.getOrNull(flags shr 4),
                           burstID = "bt:$burst", rising = flags and 1 != 0, call = flags and 2 != 0)
            })
            4 -> {
                if (body < 3) throw FrameError("reply length")
                val action = b.get().toInt() and 0xFF
                val ok = (b.get().toInt() and 0xFF) == 1
                val n = b.get().toInt() and 0xFF
                if (body != 3 + n) throw FrameError("reply length")
                val text = ByteArray(n).also { b.get(it) }.toString(Charsets.UTF_8)
                Frame.Reply(boot, counter, Action.entries.firstOrNull { it.code == action }, ok, text)
            }
            else -> throw FrameError("kind $kind")
        }
    }

    /** A short public name for a key: the first 8 hex digits of its SHA-256. */
    fun keyId(key: ByteArray): String = sha256(key).copyOf(4).hex()

    private fun <T> rows(b: ByteBuffer, body: Int, size: Int, read: ByteBuffer.() -> T): List<T> {
        if (body < 1) throw FrameError("row count")
        val n = b.get().toInt() and 0xFF
        if (body != 1 + n * size) throw FrameError("row count")
        return List(n) { b.read() }
    }

    private fun ByteBuffer.u16(): Int = short.toInt() and 0xFFFF
    private fun tenths(v: Short): Double? = if (v == I16_NONE) null else v / 10.0
}

/** The key of a match id: the first 8 bytes of its SHA-256, the same on the car computer and every route. */
object MatchKey {
    fun of(id: String): String = sha256(id.toByteArray(Charsets.UTF_8)).copyOf(8).hex()

    /** The key the alert engine remembers a match by. */
    fun of(match: MatchRow): String = match.key ?: of(match.id)
}

/**
 * Which signed frames to take: within a boot only a higher counter, and never an earlier boot again, so a
 * recording of the link replayed later cannot raise an alert.
 */
class FrameAcceptor(private val remember: Int = 32) {
    private var boot: Long? = null
    private var counter = -1L
    private val old = ArrayDeque<Long>()

    fun accept(boot: Long, counter: Long): Boolean {
        if (boot == this.boot) {
            if (counter <= this.counter) return false
        } else {
            if (boot in old) return false
            this.boot?.let { old.addLast(it); if (old.size > remember) old.removeFirst() }
            this.boot = boot
        }
        this.counter = counter
        return true
    }
}

/**
 * Which route brought each match first, and by how much: the measured answer to "is Bluetooth faster here".
 * Each key is counted once, when its second route delivers it.
 */
class RouteStats(private val limit: Int = 400) {
    enum class Route { BLUETOOTH, SERVER }

    private val first = LinkedHashMap<String, Pair<Route, Double>>()
    private val leads = ArrayDeque<Double>()
    var bluetoothFirst = 0
        private set
    var serverFirst = 0
        private set
    var bluetoothOnly = 0
        private set

    fun arrived(key: String, route: Route, at: Double) {
        val seen = first[key]
        if (seen == null) {
            first[key] = route to at
            if (first.size > limit) first.remove(first.keys.first())
            return
        }
        if (seen.first == route) return
        first.remove(key)
        if (seen.first == Route.BLUETOOTH) {
            bluetoothFirst++
            leads.addLast(at - seen.second)
            if (leads.size > 200) leads.removeFirst()
        } else {
            serverFirst++
        }
    }

    /** Matches heard over Bluetooth that the server had not delivered after [wait] seconds. */
    fun settle(now: Double, wait: Double = 60.0) {
        val gone = first.filterValues { (route, at) -> route == Route.BLUETOOTH && now - at > wait }.keys
        bluetoothOnly += gone.size
        gone.forEach { first.remove(it) }
        first.entries.removeAll { (_, v) -> v.first == Route.SERVER && now - v.second > wait }
    }

    /** The median time Bluetooth was ahead of the server, in seconds, or null before any comparison. */
    val medianLead: Double?
        get() = leads.sorted().let { if (it.isEmpty()) null else it[it.size / 2] }
}

/**
 * What came over Bluetooth, for the app: the key frames are checked with, the newest detector state and channel
 * rows, and when they arrived. The app's Bluetooth service hands it the bytes; nothing here touches Bluetooth.
 */
class BluetoothFeed(key: ByteArray? = null) {
    sealed class Received {
        /** New matches, to go to the alert engine with the time they arrived. */
        data class Matches(val matches: List<MatchRow>) : Received()
        /** A detector state or channel rows; the screens can show them. */
        data object State : Received()
        /** The car computer's answer to a control frame from this phone. */
        data class Reply(val action: BluetoothFormat.Action?, val ok: Boolean, val text: String) : Received()
        data class Refused(val why: String) : Received()
    }

    var key: ByteArray? = key
        set(value) {
            field = value
            reset()
        }
    private var acceptor = FrameAcceptor()
    var detector: DetectorState? = null
        private set
    var channels: List<ChannelRow> = emptyList()
        private set
    private var statusAt = Double.NEGATIVE_INFINITY
    private var channelsAt = Double.NEGATIVE_INFINITY
    var lastFrameAt = Double.NEGATIVE_INFINITY
        private set
    var frames = 0
        private set
    var refused = 0
        private set
    var lastRefusal: String? = null
        private set
    private var lastBoot: Long? = null
    private var lastCounter = 0L

    fun receive(bytes: ByteArray, now: Double): Received {
        val k = key ?: return refuse("no key for this car computer yet")
        val frame = try {
            BluetoothFormat.decode(bytes, k)
        } catch (e: BluetoothFormat.FrameError) {
            return refuse(e.message ?: "unreadable frame")
        }
        if (!acceptor.accept(frame.boot, frame.counter)) return refuse("a frame it had already sent")
        frames++
        lastFrameAt = now
        lastBoot = frame.boot
        lastCounter = frame.counter
        return when (frame) {
            is BluetoothFormat.Frame.Reply -> Received.Reply(frame.action, frame.ok, frame.text)
            is BluetoothFormat.Frame.Status -> {
                detector = frame.detector
                statusAt = now
                Received.State
            }
            is BluetoothFormat.Frame.Channels -> {
                channels = frame.channels
                channelsAt = now
                Received.State
            }
            is BluetoothFormat.Frame.Matches -> Received.Matches(frame.matches)
        }
    }

    private fun refuse(why: String): Received {
        refused++
        lastRefusal = why
        return Received.Refused(why)
    }

    /** The car is live over Bluetooth: a fresh status that says the detector runs, by LinkTracker's rule. */
    fun live(now: Double): Boolean =
        now - statusAt <= LinkTracker.CAR_STALE && detector?.state.let { it == "running" || it == "degraded" }

    /** Any checked frame lately, whatever the detector says. */
    fun heard(now: Double): Boolean = now - lastFrameAt <= LinkTracker.CAR_STALE

    /** What the screens show when only Bluetooth brings data: the last server state with Bluetooth's detector and channels. */
    fun piState(base: PiState?, now: Double): PiState? {
        val d = detector ?: return null
        val aged = channels.map { it.copy(age = it.age + (now - channelsAt)) }
        val start = base ?: PiState(snapshotAge = 0.0, detector = d, channels = emptyList(), matches = emptyList())
        return start.copy(snapshotAge = max(0.0, now - statusAt), detector = d, channels = aged, matches = emptyList())
    }

    /** A signed control frame for the car computer, or null before any frame came from it. */
    fun control(action: BluetoothFormat.Action, name: String = ""): ByteArray? {
        val k = key ?: return null
        val boot = lastBoot ?: return null
        return BluetoothFormat.control(k, boot, lastCounter, action, name)
    }

    fun reset() {
        lastBoot = null
        acceptor = FrameAcceptor()
        detector = null
        channels = emptyList()
        statusAt = Double.NEGATIVE_INFINITY
        channelsAt = Double.NEGATIVE_INFINITY
    }
}

/** The name phones show for a car computer: the rule p0/bluetooth_config.py and the server use. */
object BluetoothName {
    const val DEFAULT = "Slot17"
    const val MAX = 20
    private val pattern = Regex("[A-Za-z0-9][A-Za-z0-9 ._-]*")

    fun valid(name: String): Boolean = name.length in 1..MAX && name == name.trim() && pattern.matches(name)
}

/** The car computer's Bluetooth state in plain words. */
object BluetoothWords {
    fun car(b: BluetoothState?): String = when {
        b == null -> "The car computer has not reported its Bluetooth yet"
        !b.on -> "Off"
        b.state == "connected" -> "A phone is connected"
        b.state == "advertising" -> "On, waiting for a phone"
        b.state == "starting" -> "Starting"
        b.state == "unavailable" -> "Not available: ${b.error ?: "no Bluetooth adapter"}"
        b.state == "error" -> "Problem: ${b.error ?: "unknown"}"
        else -> b.state
    }
}

internal fun hmac(key: ByteArray, data: ByteArray): ByteArray =
    Mac.getInstance("HmacSHA256").apply { init(SecretKeySpec(key, "HmacSHA256")) }.doFinal(data)

internal fun sha256(data: ByteArray): ByteArray = MessageDigest.getInstance("SHA-256").digest(data)

internal fun ByteArray.hex(): String = joinToString("") { "%02x".format(it.toInt() and 0xFF) }

/** Bytes from hex digits, or null when they are not hex. */
fun hexBytes(text: String): ByteArray? {
    if (text.length % 2 != 0) return null
    return runCatching { ByteArray(text.length / 2) { text.substring(2 * it, 2 * it + 2).toInt(16).toByte() } }.getOrNull()
}
