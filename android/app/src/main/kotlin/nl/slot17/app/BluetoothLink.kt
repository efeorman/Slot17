package nl.slot17.app

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.companion.AssociationInfo
import android.companion.AssociationRequest
import android.companion.BluetoothLeDeviceFilter
import android.companion.CompanionDeviceManager
import android.content.Context
import android.content.Intent
import android.content.IntentSender
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.os.SystemClock
import java.util.UUID
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import nl.slot17.core.BluetoothFormat
import nl.slot17.core.ProtocolJson

/**
 * The phone's side of the car computer's Bluetooth link (docs/design/BLUETOOTH.md): the car computer is chosen
 * once in Android's companion device chooser, then a GATT connection that Android makes again by itself
 * (`autoConnect`) whenever the car computer is in range hands every notification to [onFrame].
 *
 * Every call checks BLUETOOTH_CONNECT first ([permitted]). Callbacks arrive on a binder thread and are passed to
 * the main thread; one GATT operation runs at a time, as Android requires.
 */
@SuppressLint("MissingPermission")
class BluetoothLink(
    private val context: Context,
    private val onFrame: (ByteArray) -> Unit,
    private val onChange: () -> Unit,
    private val note: (String) -> Unit,
) {
    enum class State { OFF, NO_PERMISSION, CONNECTING, CONNECTED, WRONG_CAR }

    var state = State.OFF
        private set
    var mtu: Int? = null
        private set
    /** What this car computer's link can do beyond 1.4.0, from its info: "state", "ops", "files" (1.6.0). */
    var can: Set<String> = emptySet()
        private set

    private val main = Handler(Looper.getMainLooper())
    private var gatt: BluetoothGatt? = null
    private var expectedKeyId: String? = null
    private val pending = ArrayDeque<(BluetoothGatt) -> Boolean>()
    private var busy = false

    fun permitted(): Boolean = Build.VERSION.SDK_INT < 31 ||
        context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED

    /** Shows Android's chooser with the car computers that advertise the Slot17 service. [chosen] gets the address. */
    @Suppress("DEPRECATION")
    fun choose(show: (IntentSender) -> Unit, chosen: (String) -> Unit, failed: (String) -> Unit) {
        val cdm = context.getSystemService(CompanionDeviceManager::class.java)
        if (cdm == null) {
            failed("This phone cannot pair companion devices")
            return
        }
        val filter = BluetoothLeDeviceFilter.Builder()
            .setScanFilter(ScanFilter.Builder().setServiceUuid(ParcelUuid(SERVICE)).build()).build()
        val request = AssociationRequest.Builder().addDeviceFilter(filter).setSingleDevice(false).build()
        val callback = object : CompanionDeviceManager.Callback() {
            override fun onAssociationPending(intentSender: IntentSender) {
                main.post { show(intentSender) }
            }

            @Deprecated("Android 12L and lower")
            override fun onDeviceFound(intentSender: IntentSender) {
                main.post { show(intentSender) }
            }

            override fun onAssociationCreated(associationInfo: AssociationInfo) {
                val address = if (Build.VERSION.SDK_INT >= 33) associationInfo.deviceMacAddress?.toString()?.uppercase() else null
                if (address != null) main.post { chosen(address) }
            }

            override fun onFailure(error: CharSequence?) {
                main.post { failed(error?.toString() ?: "No car computer was chosen") }
            }
        }
        if (Build.VERSION.SDK_INT >= 33) cdm.associate(request, context.mainExecutor, callback) else cdm.associate(request, callback, main)
    }

    /** The chooser's answer where it comes back as an activity result (Android 12L and lower). */
    @Suppress("DEPRECATION")
    fun chosenFrom(data: Intent?): String? {
        val result: ScanResult? = if (Build.VERSION.SDK_INT >= 33) {
            data?.getParcelableExtra(CompanionDeviceManager.EXTRA_DEVICE, ScanResult::class.java)
        } else {
            data?.getParcelableExtra(CompanionDeviceManager.EXTRA_DEVICE)
        }
        return result?.device?.address
    }

    /** Connects to the chosen car computer and keeps connecting. [keyId] says which car computer is expected. */
    fun connect(address: String, keyId: String?) {
        expectedKeyId = keyId
        if (!permitted()) {
            set(State.NO_PERMISSION)
            return
        }
        if (gatt?.device?.address.equals(address, ignoreCase = true) && state != State.WRONG_CAR) return
        val adapter = context.getSystemService(BluetoothManager::class.java)?.adapter
        val device: BluetoothDevice = runCatching { adapter?.getRemoteDevice(address) }.getOrNull() ?: return
        close()
        set(State.CONNECTING)
        gatt = device.connectGatt(context, true, callback, BluetoothDevice.TRANSPORT_LE)
    }

    fun close() {
        pending.clear()
        busy = false
        gatt?.let {
            if (permitted()) {
                it.disconnect()
                it.close()
            }
        }
        gatt = null
        mtu = null
        can = emptySet()
        set(State.OFF)
    }

    /** Ends the link and removes Android's association with the car computer. */
    @Suppress("DEPRECATION")
    fun forget(address: String?) {
        close()
        val cdm = context.getSystemService(CompanionDeviceManager::class.java) ?: return
        runCatching {
            if (Build.VERSION.SDK_INT >= 33) {
                cdm.myAssociations.filter { it.deviceMacAddress?.toString().equals(address, ignoreCase = true) }
                    .forEach { cdm.disassociate(it.id) }
            } else if (address != null) {
                cdm.disassociate(address)
            }
        }
    }

    private fun set(s: State) {
        if (s != state) {
            state = s
            main.post(onChange)
        }
    }

    /** Runs GATT operations one after another; an operation that could not start returns false. */
    private fun queue(op: (BluetoothGatt) -> Boolean) {
        pending.addLast(op)
        next()
    }

    private fun next() {
        val g = gatt ?: return
        while (!busy && pending.isNotEmpty()) busy = pending.removeFirst()(g)
    }

    private fun done() {
        busy = false
        next()
    }

    private var lastProblemAt = -RETRY_MS * 100

    /**
     * Something went wrong between connecting and the first frame: say so (once a minute at most) and connect
     * again a moment later. Before 1.5.1 the link stayed connected and silent, or worse, see [infoRead].
     */
    private fun retry(g: BluetoothGatt, why: String) {
        val now = SystemClock.elapsedRealtime()
        if (now - lastProblemAt >= 60_000) {
            lastProblemAt = now
            note("PROBLEM Bluetooth: $why; connecting again")
        }
        val address = g.device?.address ?: return
        main.postDelayed({
            if (g === gatt && state != State.WRONG_CAR) {
                close()
                connect(address, expectedKeyId)
            }
        }, RETRY_MS)
    }

    /** Writes a control frame (switch off, rename) to the car computer. False when there is no link to write on. */
    fun write(data: ByteArray): Boolean = write(listOf(data))

    /** Writes control frames in order, one GATT write each, so the car computer gets every piece of an operation. */
    fun write(frames: List<ByteArray>): Boolean {
        if (state != State.CONNECTED || !permitted() || frames.isEmpty()) return false
        for (data in frames) {
            val copy = data.copyOf()
            queue { g ->
                val c = g.getService(SERVICE)?.getCharacteristic(CONTROL)
                if (c == null) false else writeControl(g, c, copy)
            }
        }
        return true
    }

    @Suppress("DEPRECATION")
    private fun writeControl(g: BluetoothGatt, c: BluetoothGattCharacteristic, data: ByteArray): Boolean =
        if (Build.VERSION.SDK_INT >= 33) {
            g.writeCharacteristic(c, data, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothStatusCodes.SUCCESS
        } else {
            c.value = data
            c.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            g.writeCharacteristic(c)
        }

    @Suppress("DEPRECATION")
    private fun enableNotifications(g: BluetoothGatt, cccd: BluetoothGattDescriptor): Boolean =
        if (Build.VERSION.SDK_INT >= 33) {
            g.writeDescriptor(cccd, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE) == BluetoothGatt.GATT_SUCCESS
        } else {
            cccd.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            g.writeDescriptor(cccd)
        }

    private fun infoRead(g: BluetoothGatt, value: ByteArray, status: Int) {
        done()
        // A read that failed (while a pairing was pending, for one) has no key id. Before 1.5.1 that was taken
        // for a different car computer, which only "Set up again" undid.
        if (status != BluetoothGatt.GATT_SUCCESS) {
            retry(g, "reading the car computer failed (status $status)")
            return
        }
        val info = runCatching { ProtocolJson.parseToJsonElement(value.decodeToString()).jsonObject }.getOrNull()
        val keyId = info?.get("key_id")?.jsonPrimitive?.content
        can = runCatching { info?.get("can")?.jsonArray?.map { it.jsonPrimitive.content }?.toSet() }.getOrNull() ?: emptySet()
        if (expectedKeyId != null && keyId != expectedKeyId) {
            note("PROBLEM Bluetooth: this is a different car computer (key $keyId); set it up again")
            set(State.WRONG_CAR)
            return
        }
        val frames = g.getService(SERVICE)?.getCharacteristic(FRAMES) ?: return
        val cccd = frames.getDescriptor(CCCD) ?: return
        g.setCharacteristicNotification(frames, true)
        queue { enableNotifications(it, cccd) }
    }

    private val callback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            main.post {
                if (g !== gatt) return@post
                pending.clear()
                busy = false
                if (newState == BluetoothProfile.STATE_CONNECTED) {
                    // The MTU first, so a frame fits one notification; a phone that refuses still gets the services.
                    queue { it.requestMtu(517) }
                    queue { it.discoverServices() }
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                    mtu = null
                    if (state != State.WRONG_CAR) set(State.CONNECTING)   // autoConnect makes it again when in range
                }
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, value: Int, status: Int) {
            main.post {
                mtu = value
                done()
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            main.post {
                done()
                val info = g.getService(SERVICE)?.getCharacteristic(INFO)
                // Also while the car computer's own Bluetooth link starts again: its service is back moments later.
                if (info == null) retry(g, "the car computer has no Slot17 service yet (status $status)")
                else queue { it.readCharacteristic(info) }
            }
        }

        @Deprecated("Android 12L and lower")
        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            @Suppress("DEPRECATION") val value = c.value?.copyOf() ?: ByteArray(0)
            main.post { infoRead(g, value, status) }
        }

        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray, status: Int) {
            val copy = value.copyOf()
            main.post { infoRead(g, copy, status) }
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            main.post {
                done()
                if (status != BluetoothGatt.GATT_SUCCESS) note("PROBLEM Bluetooth: the car computer refused a command (status $status)")
            }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            main.post {
                done()
                if (status == BluetoothGatt.GATT_SUCCESS) {
                    set(State.CONNECTED)
                    note("Bluetooth connected to the car computer (MTU ${mtu ?: "default"})")
                } else {
                    retry(g, "no notifications (status $status)")
                }
            }
        }

        @Deprecated("Android 12L and lower")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            @Suppress("DEPRECATION") val value = c.value?.copyOf() ?: return
            main.post { onFrame(value) }
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            val copy = value.copyOf()
            main.post { onFrame(copy) }
        }
    }

    companion object {
        val SERVICE: UUID = UUID.fromString(BluetoothFormat.SERVICE_UUID)
        val FRAMES: UUID = UUID.fromString(BluetoothFormat.FRAMES_UUID)
        val INFO: UUID = UUID.fromString(BluetoothFormat.INFO_UUID)
        val CONTROL: UUID = UUID.fromString(BluetoothFormat.CONTROL_UUID)
        val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        /** Milliseconds before a link that went wrong is tried again. */
        const val RETRY_MS = 4_000L
    }
}
