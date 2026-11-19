package nl.slot17.app

import android.content.Context
import android.os.SystemClock
import java.io.File
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import kotlinx.serialization.KSerializer
import nl.slot17.core.ProtocolJson

/** Seconds on a clock that keeps counting while the phone sleeps, so ages only ever overstate. */
fun mono(): Double = SystemClock.elapsedRealtimeNanos() / 1e9

private val ISO = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'").withZone(ZoneOffset.UTC)
private val STAMP = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC)

/** The ISO time Swift's JSONEncoder writes, which the server's drive report reads. */
fun isoTime(ms: Long): String = ISO.format(Instant.ofEpochMilli(ms))

/** The app's own log: recent lines in memory, everything in a file the Logs screen can send. */
class EventLog(private val file: File) {
    private val recentLines = ArrayDeque<String>()
    val recent: List<String> get() = synchronized(this) { recentLines.toList() }

    fun write(text: String) {
        val line = "${STAMP.format(Instant.now())} $text"
        synchronized(this) {
            recentLines.addLast(line)
            while (recentLines.size > 300) recentLines.removeFirst()
            runCatching {
                if (file.length() > MAX_FILE) {
                    val keep = tailBytes(MAX_FILE / 2)
                    file.writeBytes(keep)
                }
                file.appendText(line + "\n")
            }
        }
    }

    fun tail(maxBytes: Int): ByteArray = synchronized(this) { tailBytes(maxBytes.toLong()) }

    private fun tailBytes(max: Long): ByteArray {
        if (!file.exists()) return ByteArray(0)
        val all = file.readBytes()
        if (all.size <= max) return all
        var start = (all.size - max).toInt()
        while (start < all.size && all[start] != '\n'.code.toByte()) start++
        return all.copyOfRange(minOf(start + 1, all.size), all.size)
    }

    fun clear() = synchronized(this) {
        recentLines.clear()
        runCatching { file.writeText("") }
    }

    companion object {
        private const val MAX_FILE = 4L * 1024 * 1024
    }
}

/** Small things saved on the phone: settings and history as JSON files, the rest in preferences. */
class Store(context: Context) {
    private val dir = context.filesDir
    val prefs = context.getSharedPreferences("slot17", Context.MODE_PRIVATE)

    fun <T> load(name: String, serializer: KSerializer<T>): T? = runCatching {
        val f = File(dir, name)
        if (f.exists()) ProtocolJson.decodeFromString(serializer, f.readText()) else null
    }.getOrNull()

    /** Written whole to a temporary file first, so a crash never leaves half a file. */
    fun <T> save(name: String, serializer: KSerializer<T>, value: T): Boolean = runCatching {
        val tmp = File(dir, "$name.tmp")
        tmp.writeText(ProtocolJson.encodeToString(serializer, value))
        if (!tmp.renameTo(File(dir, name))) {
            File(dir, name).delete()
            tmp.renameTo(File(dir, name))
        } else true
    }.getOrDefault(false)

    fun readText(name: String): String? = runCatching { File(dir, name).takeIf { it.exists() }?.readText() }.getOrNull()
    fun writeText(name: String, text: String) { runCatching { File(dir, name).writeText(text) } }
    fun delete(name: String) { File(dir, name).delete() }

    /** The private link. App-private storage, excluded from backups (allowBackup is off). */
    var link: String?
        get() = prefs.getString("link", null)
        set(value) { prefs.edit().apply { if (value == null) remove("link") else putString("link", value) }.apply() }
}
