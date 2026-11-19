package nl.slot17.app

import android.app.UiModeManager
import android.content.Context
import android.content.res.Configuration
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Handler
import android.os.Looper
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.EnumMap
import kotlin.math.abs
import kotlin.math.log10
import nl.slot17.core.BeepStyle
import nl.slot17.core.SoundKind
import nl.slot17.core.SoundSynth

/**
 * Plays the app's sounds. The Android side of ios/Slot17/Services/AudioAlerts.swift.
 *
 * Every sound goes out as navigation guidance on the media stream: it follows the phone's audio to
 * the car (Bluetooth or Android Auto) and uses the media volume, like a navigation app's voice.
 * "Over music" asks for no audio focus, "Lower music" and "Voice prompt" ask for a short ducking focus.
 */
class SoundPlayer(context: Context, private val log: (String) -> Unit) {
    class Sound(val samples: FloatArray, val rate: Int)

    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val uiMode = context.getSystemService(Context.UI_MODE_SERVICE) as UiModeManager
    private val main = Handler(Looper.getMainLooper())
    private val sounds = EnumMap<SoundKind, Sound>(SoundKind::class.java)

    var volume = 1f
    var style = BeepStyle.MIX
    var armed = false
        private set
    var soundsPlayed = 0
        private set
    var lastPeakDB: Double? = null
        private set
    var onPlayed: (SoundKind, Double) -> Unit = { _, _ -> }

    init {
        for (kind in SoundKind.entries) sounds[kind] = Sound(SoundSynth.render(kind.builtIn), 44_100)
    }

    fun setSound(kind: SoundKind, sound: Sound) { sounds[kind] = sound }

    fun arm() {
        if (!armed) log("sound armed; output $routeName")
        armed = true
    }

    fun disarm(why: String) {
        if (armed) log("sound disarmed: $why")
        armed = false
    }

    /** Why no sound can be heard right now, or null. */
    val problem: String?
        get() = when {
            audio.getStreamVolume(AudioManager.STREAM_MUSIC) == 0 -> "the media volume is at zero"
            else -> null
        }

    private val inCall: Boolean
        get() = audio.mode == AudioManager.MODE_IN_CALL || audio.mode == AudioManager.MODE_IN_COMMUNICATION

    val otherAudioPlaying: Boolean get() = audio.isMusicActive

    /** Android Auto switches the phone to car mode. */
    val routeIsCar: Boolean get() = uiMode.currentModeType == Configuration.UI_MODE_TYPE_CAR

    val routeName: String
        get() {
            val outputs = audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
            val order = listOf(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, AudioDeviceInfo.TYPE_USB_HEADSET, AudioDeviceInfo.TYPE_USB_DEVICE,
                AudioDeviceInfo.TYPE_WIRED_HEADPHONES, AudioDeviceInfo.TYPE_WIRED_HEADSET, AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)
            val device = order.firstNotNullOfOrNull { type -> outputs.firstOrNull { it.type == type } }
            val kind = when (device?.type) {
                AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "Bluetooth"
                AudioDeviceInfo.TYPE_USB_HEADSET, AudioDeviceInfo.TYPE_USB_DEVICE -> "USB"
                AudioDeviceInfo.TYPE_WIRED_HEADPHONES, AudioDeviceInfo.TYPE_WIRED_HEADSET -> "headphones"
                AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "speaker"
                else -> "unknown"
            }
            val name = device?.productName?.toString()?.takeIf { it.isNotBlank() && device.type != AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
            return (if (name != null) "$kind ($name)" else kind) + if (routeIsCar) ", car mode" else ""
        }

    /** Plays now, or says why it cannot. Never queued to play late. */
    fun play(kind: SoundKind): String? {
        if (inCall) return "a phone call is using the audio"
        val sound = sounds[kind] ?: return "no sound loaded"
        val pcm = toPcm(sound.samples, volume)
        return try {
            val attributes = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ASSISTANCE_NAVIGATION_GUIDANCE)
                .setContentType(if (style == BeepStyle.PROMPT) AudioAttributes.CONTENT_TYPE_SPEECH else AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build()
            val track = AudioTrack.Builder()
                .setAudioAttributes(attributes)
                .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(sound.rate).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                .setTransferMode(AudioTrack.MODE_STATIC)
                .setBufferSizeInBytes(pcm.size)
                .build()
            track.write(pcm, 0, pcm.size)
            val focus = if (style == BeepStyle.MIX) null else AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                .setAudioAttributes(attributes).build()
            focus?.let { audio.requestAudioFocus(it) }
            // Lowering the music takes a moment; the beep starts once it has.
            val delay = if (focus != null) 250L else 0L
            main.postDelayed({ runCatching { track.play() } }, delay)
            val lengthMs = sound.samples.size * 1000L / sound.rate
            main.postDelayed({
                runCatching { track.stop() }
                track.release()
                focus?.let { audio.abandonAudioFocusRequest(it) }
            }, delay + lengthMs + 300)
            soundsPlayed++
            val peak = sound.samples.maxOfOrNull { abs(it) }?.times(volume) ?: 0f
            lastPeakDB = if (peak > 0) 20 * log10(peak.toDouble()) else null
            onPlayed(kind, lastPeakDB ?: -120.0)
            null
        } catch (e: Exception) {
            "the sound could not start: ${e.message}"
        }
    }

    companion object {
        fun toPcm(samples: FloatArray, volume: Float): ByteArray {
            val out = ByteBuffer.allocate(samples.size * 2).order(ByteOrder.LITTLE_ENDIAN)
            for (s in samples) out.putShort((s * volume).coerceIn(-1f, 1f).times(32767).toInt().toShort())
            return out.array()
        }

        /** A server sound: 16-bit PCM WAV, mono or stereo (mixed down). Null if it is anything else. */
        fun wav(data: ByteArray): Sound? {
            val b = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN)
            if (data.size < 44 || String(data, 0, 4) != "RIFF" || String(data, 8, 4) != "WAVE") return null
            var pos = 12
            var channels = 0
            var rate = 0
            var bits = 0
            var format = 0
            while (pos + 8 <= data.size) {
                val id = String(data, pos, 4)
                val size = b.getInt(pos + 4)
                val body = pos + 8
                if (size < 0 || body + size > data.size) return null
                if (id == "fmt ") {
                    format = b.getShort(body).toInt()
                    channels = b.getShort(body + 2).toInt()
                    rate = b.getInt(body + 4)
                    bits = b.getShort(body + 14).toInt()
                } else if (id == "data") {
                    if (format != 1 || bits != 16 || channels !in 1..2 || rate !in 8_000..96_000) return null
                    val frames = size / (2 * channels)
                    if (frames == 0 || frames > rate * 5) return null
                    val samples = FloatArray(frames) { i ->
                        var sum = 0f
                        for (c in 0 until channels) sum += b.getShort(body + (i * channels + c) * 2) / 32768f
                        sum / channels
                    }
                    return Sound(samples, rate)
                }
                pos = body + size + (size and 1)
            }
            return null
        }
    }
}
