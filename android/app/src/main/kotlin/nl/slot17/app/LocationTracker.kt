package nl.slot17.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Looper
import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.ListSerializer
import nl.slot17.core.Fmt
import nl.slot17.core.Place

/** One point of the drive's track, for later analysis only. */
@Serializable
data class TrackPoint(val atMs: Long, val lat: Double, val lon: Double, val speedKmh: Double? = null)

/**
 * The phone's position, recorded with alerts and sightings so later analysis knows the road and
 * the speed. Port of LocationService.swift. It never affects whether a sound plays.
 */
class LocationTracker(private val context: Context, private val store: Store, private val log: (String) -> Unit) : LocationListener {
    private val manager = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
    var trackEvery = 10.0
    var onChange: () -> Unit = {}
    var latest: Location? = null
        private set
    var running = false
        private set
    private var lowPower = false
    private var lastTrackPoint = 0L
    val track: MutableList<TrackPoint> = store.load(FILE, ListSerializer(TrackPoint.serializer()))?.toMutableList() ?: mutableListOf()

    val permitted: Boolean
        get() = context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED

    fun start() {
        if (running) return
        if (!permitted) { log("location: no permission"); onChange(); return }
        running = true
        request()
        log("location on")
        onChange()
    }

    fun stop() {
        if (!running) return
        running = false
        manager.removeUpdates(this)
        saveTrack()
        log("location off")
        onChange()
    }

    /** With no car data, a coarse and slow setting; back to GPS when data returns. */
    fun setLowPower(on: Boolean) {
        if (on == lowPower) return
        lowPower = on
        if (running) { manager.removeUpdates(this); request() }
    }

    @SuppressLint("MissingPermission")
    private fun request() {
        if (!permitted) return
        val provider = if (lowPower || !manager.isProviderEnabled(LocationManager.GPS_PROVIDER)) LocationManager.NETWORK_PROVIDER
            else LocationManager.GPS_PROVIDER
        val every = if (lowPower) 60_000L else (trackEvery * 1000).toLong().coerceAtLeast(2000)
        runCatching { manager.requestLocationUpdates(provider, every, 0f, this, Looper.getMainLooper()) }
            .onFailure { log("PROBLEM location could not start: ${it.message}") }
    }

    override fun onLocationChanged(location: Location) {
        latest = location
        if (location.time - lastTrackPoint >= trackEvery * 1000) {
            lastTrackPoint = location.time
            track.add(TrackPoint(location.time, location.latitude, location.longitude,
                if (location.hasSpeed()) location.speed * 3.6 else null))
            if (track.size > 20_000) track.subList(0, track.size - 20_000).clear()
        }
        onChange()
    }

    @Deprecated("Deprecated in Android")
    override fun onStatusChanged(provider: String?, status: Int, extras: android.os.Bundle?) {}
    override fun onProviderEnabled(provider: String) {}
    override fun onProviderDisabled(provider: String) {}

    /** Where the phone is now, if the fix is fresh enough to mean anything. */
    val place: Place?
        get() {
            val l = latest ?: return null
            if (System.currentTimeMillis() - l.time > 30_000) return null
            return Place(l.latitude, l.longitude, if (l.hasSpeed()) l.speed * 3.6 else null,
                if (l.hasAccuracy()) l.accuracy.toDouble() else null)
        }

    val statusText: String
        get() = when {
            !permitted -> "No permission"
            !running -> "Off"
            latest == null -> "Waiting for a fix"
            else -> {
                val l = latest!!
                val age = (System.currentTimeMillis() - l.time) / 1000.0
                "±${Fmt.fixed(l.accuracy.toDouble(), 0)} m, ${nl.slot17.core.Format.ago(age)}"
            }
        }

    fun saveTrack() { store.save(FILE, ListSerializer(TrackPoint.serializer()), track) }

    fun clearTrack() { track.clear(); store.delete(FILE) }

    companion object {
        private const val FILE = "track.json"
    }
}
