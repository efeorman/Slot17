package nl.slot17.app

import android.content.Context
import android.content.Intent
import android.content.IntentSender
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import nl.slot17.core.BluetoothFeed
import nl.slot17.core.BluetoothFormat
import nl.slot17.core.BluetoothName
import nl.slot17.core.BluetoothState
import nl.slot17.core.Level
import nl.slot17.core.MatchRow
import nl.slot17.core.OpStatus
import nl.slot17.core.PiState
import nl.slot17.core.RouteStats
import nl.slot17.core.hexBytes

/** This phone's side of the Bluetooth route, saved between starts. The key is the car computer's frame key. */
@Serializable
data class PhoneBluetooth(val key: String? = null, val keyId: String? = null, val address: String? = null, val use: Boolean = true)

/**
 * The Bluetooth route as the app sees it: the car computer's switch and name (requests through the server, like
 * the Pushover switch), this phone's link to it, the frames it brings and how they compare with the server's.
 * [AppModel] owns one; [deliver] hands new matches to the alert engine with the time they arrived.
 */
class BluetoothRoute(
    private val context: Context,
    private val store: Store,
    private val sendOp: (String, JsonObject, (ActionState) -> Unit) -> Unit,
    private val carState: () -> PiState?,
    private val deliver: (List<MatchRow>, Double) -> Unit,
    private val changed: () -> Unit,
    private val note: (String) -> Unit,
) {
    var phone by mutableStateOf(store.load(FILE, PhoneBluetooth.serializer()) ?: PhoneBluetooth())
        private set
    val feed = BluetoothFeed(phone.key?.let(::hexBytes))
    val stats = RouteStats()
    val link = BluetoothLink(context, ::frame, { changed() }, note)

    /** The car computer's switch and rename requests, and the pairing request. */
    var carAction by mutableStateOf(ActionState())
        private set
    var pairing by mutableStateOf(ActionState())
        private set
    /** The companion chooser to show; the Settings screen launches it and clears this. */
    var chooser by mutableStateOf<IntentSender?>(null)
    var wanted by mutableStateOf<Boolean?>(null)
        private set
    private var lastRefusalNote = Double.NEGATIVE_INFINITY

    val onCar: BluetoothState? get() = carState()?.bluetooth
    val shownOn: Boolean get() = wanted ?: onCar?.on ?: false
    val paired: Boolean get() = phone.key != null && phone.address != null

    /** Why the car computer's switch cannot be used, or null. */
    val unavailable: String?
        get() {
            val state = carState() ?: return "The car computer has not reported yet"
            if ("bluetooth" !in (state.features ?: emptyList())) return "The car computer needs software 1.4.0 or later for this"
            return null
        }

    // --- the car computer ---------------------------------------------------------------------------

    /** On goes through the server (a link that is off hears nothing); off goes over Bluetooth when it is connected. */
    fun setOnCar(on: Boolean) {
        if (unavailable != null && !overBluetooth) return
        wanted = on
        note("Bluetooth on the car computer ${if (on) "on" else "off"} requested")
        if (!on && control(BluetoothFormat.Action.OFF)) return
        sendOp("bluetooth", JsonObject(mapOf("on" to JsonPrimitive(on)))) { carAction = it }
    }

    /** Renames the car computer as phones show it: over Bluetooth when connected, else through the server.
     *  Returns why not, or null when the request went out. */
    fun rename(name: String): String? {
        val clean = name.trim()
        if (!BluetoothName.valid(clean)) return "Use 1 to ${BluetoothName.MAX} letters, digits, spaces, '.', '-' or '_'"
        note("Bluetooth name \"$clean\" requested")
        if (control(BluetoothFormat.Action.RENAME, clean)) return null
        sendOp("bluetooth", JsonObject(mapOf("name" to JsonPrimitive(clean)))) { carAction = it }
        return null
    }

    /** The link is up and a frame has come from the car computer, so a signed command can go straight to it. */
    val overBluetooth: Boolean get() = link.state == BluetoothLink.State.CONNECTED && feed.heard(mono())

    private fun control(action: BluetoothFormat.Action, name: String = ""): Boolean {
        if (!overBluetooth) return false
        val frame = feed.control(action, name) ?: return false
        if (!link.write(frame)) return false
        carAction = ActionState("bluetooth-${action.name.lowercase()}", "Sent over Bluetooth…", Level.IDLE, true)
        return true
    }

    fun settleWanted() {
        val w = wanted ?: return
        if (carAction.busy) return
        if (onCar?.on == w || carAction.level == Level.BAD) wanted = null
    }

    // --- this phone --------------------------------------------------------------------------------

    /** Step 1 of setting up: the car computer's key, through the server. Step 2 is the chooser. */
    fun startPairing() {
        if (unavailable != null) return
        if (onCar?.on != true) {
            pairing = ActionState(text = "Switch Bluetooth on for the car computer first", level = Level.BAD)
            return
        }
        note("Bluetooth set-up started")
        sendOp("bluetooth", JsonObject(mapOf("pair" to JsonPrimitive(true)))) { pairing = it }
    }

    /** The server's answers for this route's requests. The key in a pairing answer is never shown or logged. */
    fun apply(op: OpStatus) {
        if (op.id == carAction.id) {
            carAction = when (op.state) {
                "done" -> ActionState(op.id, op.detail.ifEmpty { "Saved on the car computer" }, Level.OK)
                "failed", "expired" -> ActionState(op.id, op.detail.ifEmpty { "It did not work" }, Level.BAD)
                else -> ActionState(op.id, "Waiting for the car computer…", Level.IDLE, true)
            }
        } else if (op.id == pairing.id && pairing.busy) {
            when (op.state) {
                "done" -> keyArrived(op.id, op.detail)
                "failed", "expired" -> pairing = ActionState(op.id, op.detail.ifEmpty { "It did not work" }, Level.BAD)
                else -> pairing = ActionState(op.id, "Waiting for the car computer…", Level.IDLE, true)
            }
        }
    }

    private fun keyArrived(id: String, detail: String) {
        val key = detail.removePrefix("key:").takeIf { detail.startsWith("key:") }?.let(::hexBytes)
        if (key == null || key.size != BluetoothFormat.KEY_BYTES) {
            pairing = ActionState(id, "The car computer's answer could not be read", Level.BAD)
            return
        }
        save(phone.copy(key = key.toHex(), keyId = BluetoothFormat.keyId(key)))
        feed.key = key
        pairing = ActionState(id, "Choose the car computer in the list Android shows…", Level.IDLE, true)
        link.choose(show = { chooser = it }, chosen = ::chosen, failed = { pairing = ActionState(id, it, Level.BAD) })
    }

    /** The chooser's result, on Android 12L and lower (newer ones answer through the association itself). */
    fun chooserResult(data: Intent?) {
        link.chosenFrom(data)?.let(::chosen)
    }

    private fun chosen(address: String) {
        save(phone.copy(address = address, use = true))
        pairing = ActionState(pairing.id, "Set up. It connects whenever the car computer is in range.", Level.OK)
        note("Bluetooth set up with the car computer")
        start()
    }

    fun setUse(use: Boolean) {
        save(phone.copy(use = use))
        if (use) start() else link.close()
    }

    fun remove() {
        link.forget(phone.address)
        save(PhoneBluetooth())
        feed.key = null
        pairing = ActionState(text = "Removed from this phone", level = Level.OK)
        note("Bluetooth removed from this phone")
    }

    /** While the app listens: keep the link to the car computer, if one is set up and wanted. */
    fun start() {
        val address = phone.address ?: return
        if (phone.use && phone.key != null) link.connect(address, phone.keyId)
    }

    fun stop() = link.close()

    // --- frames ------------------------------------------------------------------------------------

    private fun frame(bytes: ByteArray) {
        val now = mono()
        when (val got = feed.receive(bytes, now)) {
            is BluetoothFeed.Received.Matches -> {
                got.matches.forEach { stats.arrived(it.key ?: return@forEach, RouteStats.Route.BLUETOOTH, now) }
                deliver(got.matches, now)
            }
            BluetoothFeed.Received.State -> changed()
            is BluetoothFeed.Received.Reply -> {
                carAction = ActionState(carAction.id, got.text, if (got.ok) Level.OK else Level.BAD)
                note("Bluetooth: the car computer answered: ${got.text}")
                changed()
            }
            is BluetoothFeed.Received.Refused -> if (now - lastRefusalNote >= 60) {
                lastRefusalNote = now
                note("PROBLEM Bluetooth frame refused: ${got.why}")
            }
        }
    }

    /** The phone side in words, for Settings. */
    val phoneText: String
        get() = when {
            !paired -> "Not set up on this phone"
            !phone.use -> "Set up, switched off on this phone"
            link.state == BluetoothLink.State.NO_PERMISSION -> "Allow Nearby devices for Slot17 in Android's settings"
            link.state == BluetoothLink.State.WRONG_CAR -> "A different car computer answered: set it up again"
            link.state == BluetoothLink.State.CONNECTED ->
                if (feed.heard(mono())) "Connected, last frame ${"%.0f".format(mono() - feed.lastFrameAt)} s ago" else "Connected"
            link.state == BluetoothLink.State.CONNECTING -> "Waiting for the car computer to be in range"
            else -> "Not connected"
        }

    private fun save(p: PhoneBluetooth) {
        phone = p
        store.save(FILE, PhoneBluetooth.serializer(), p)
    }

    private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it.toInt() and 0xFF) }

    companion object {
        private const val FILE = "bluetooth.json"
    }
}
