package nl.slot17.app

import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import nl.slot17.core.ProtocolJson
import nl.slot17.core.ServerLink
import nl.slot17.core.ServerLogEntry
import nl.slot17.core.ServerMessage
import nl.slot17.core.SseParser
import nl.slot17.core.SupportListing
import nl.slot17.core.SupportPackage

/** A non-200 answer from the server. */
class HttpStatus(val code: Int) : IOException("the server answered $code")

/**
 * All traffic to the display server. Port of ios/Slot17/Services/ServerClient.swift: the same paths,
 * the same cookie, the same timeouts. Callbacks arrive on the main thread.
 */
class ServerClient(
    private val scope: CoroutineScope,
    private val onMessage: (ServerMessage) -> Unit,
    private val onStreamEvent: (String) -> Unit,
    private val onUnauthorised: () -> Unit,
) {
    @Volatile var link: ServerLink? = null
        private set
    var streamOpens = 0
        private set
    var openedAt = 0.0
        private set
    private var stream: Job? = null
    @Volatile private var streamConnection: HttpURLConnection? = null

    fun configure(link: ServerLink?) {
        this.link = link
        closeStream()
    }

    private fun open(path: String, method: String = "GET", timeoutS: Int = 12): HttpURLConnection? {
        val l = link ?: return null
        val c = URL("${l.base}/$path").openConnection() as HttpURLConnection
        c.requestMethod = method
        c.instanceFollowRedirects = false
        c.useCaches = false
        c.connectTimeout = timeoutS * 1000
        c.readTimeout = timeoutS * 1000
        c.setRequestProperty("Cookie", "s17=${l.key}")
        c.setRequestProperty("User-Agent", "Slot17-Android")
        return c
    }

    // Live stream

    fun openStream() {
        closeStream()
        val c = open("api/stream", timeoutS = 20) ?: return
        openedAt = mono()
        streamOpens++
        streamConnection = c
        stream = scope.launch(Dispatchers.IO) {
            val parser = SseParser()
            try {
                val status = c.responseCode
                if (status == 401) withContext(Dispatchers.Main) { onUnauthorised() }
                if (status != 200) {
                    withContext(Dispatchers.Main) { onStreamEvent("stream answered $status") }
                    return@launch
                }
                val input = c.inputStream
                val buffer = ByteArray(16 * 1024)
                while (isActive) {
                    val n = input.read(buffer)
                    if (n < 0) break
                    val messages = parser.feed(buffer.copyOf(n)).mapNotNull { ServerMessage.decode(it) }
                    if (messages.isNotEmpty()) withContext(Dispatchers.Main) { messages.forEach(onMessage) }
                }
            } catch (e: IOException) {
                if (isActive && streamConnection === c) withContext(Dispatchers.Main) { onStreamEvent("stream ended: ${e.message}") }
            } finally {
                c.disconnect()
            }
        }
    }

    fun closeStream() {
        stream?.cancel()
        stream = null
        streamConnection?.let { c -> scope.launch(Dispatchers.IO) { c.disconnect() } }
        streamConnection = null
    }

    // Requests

    data class Probe(val message: ServerMessage?, val error: Throwable?, val begin: Double, val end: Double)

    /** A timed request. Its duration bounds the clock offset, so it is measured here. */
    suspend fun probe(): Probe? = withContext(Dispatchers.IO) {
        val c = open("api/state", timeoutS = 4) ?: return@withContext null
        val begin = mono()
        try {
            val status = c.responseCode
            val body = if (status == 200) c.inputStream.readBytes().decodeToString() else null
            val end = mono()
            when {
                status != 200 -> Probe(null, HttpStatus(status), begin, end)
                else -> ServerMessage.decode(body ?: "")?.let { Probe(it, null, begin, end) }
                    ?: Probe(null, IOException("unreadable answer"), begin, end)
            }
        } catch (e: IOException) {
            Probe(null, e, begin, mono())
        } finally {
            c.disconnect()
        }
    }

    private suspend fun perform(c: HttpURLConnection?, body: ByteArray? = null, type: String? = null): Result<ByteArray> =
        withContext(Dispatchers.IO) {
            if (c == null) return@withContext Result.failure(IOException("no private link"))
            try {
                if (body != null) {
                    c.doOutput = true
                    c.setRequestProperty("Content-Type", type ?: "application/json")
                    c.setFixedLengthStreamingMode(body.size)
                    c.outputStream.use { it.write(body) }
                }
                val status = c.responseCode
                if (status != 200) Result.failure(HttpStatus(status)) else Result.success(c.inputStream.readBytes())
            } catch (e: IOException) {
                Result.failure(e)
            } finally {
                c.disconnect()
            }
        }

    suspend fun send(path: String, json: JsonObject): Result<ByteArray> =
        perform(open(path, "POST", 8), json.toString().encodeToByteArray())

    suspend fun uploadLog(body: ByteArray): Result<ByteArray> =
        perform(open("api/app-log", "POST", 60), body, "text/plain; charset=utf-8")

    @Serializable private data class Listing(val logs: List<ServerLogEntry>)

    suspend fun listLogs(): Result<List<ServerLogEntry>> = perform(open("api/logs", timeoutS = 10)).mapCatching {
        ProtocolJson.decodeFromString(Listing.serializer(), it.decodeToString()).logs
    }

    suspend fun fetchLog(name: String): Result<String> = perform(open("api/logs/$name", timeoutS = 30)).map { it.decodeToString() }

    /** The app's settings file from the server. 404 means the server has none: keep the defaults. */
    suspend fun fetchConfig(): Result<String> = perform(open("api/config", timeoutS = 10)).map { it.decodeToString() }

    suspend fun fetchSound(name: String): Result<ByteArray> = perform(open("api/sounds/$name", timeoutS = 20))

    // Support packages (1.5.0)

    suspend fun listSupport(): Result<List<SupportPackage>> = perform(open("api/support", timeoutS = 10)).mapCatching {
        ProtocolJson.decodeFromString(SupportListing.serializer(), it.decodeToString()).packages
    }

    /** A package to a file, streamed: it can be 100 MB. */
    suspend fun downloadSupport(name: String, dest: java.io.File): Result<java.io.File> = withContext(Dispatchers.IO) {
        val c = open("api/support/$name", timeoutS = 120) ?: return@withContext Result.failure(IOException("no private link"))
        try {
            val status = c.responseCode
            if (status != 200) return@withContext Result.failure(HttpStatus(status))
            dest.parentFile?.mkdirs()
            c.inputStream.use { input -> dest.outputStream().use { input.copyTo(it) } }
            Result.success(dest)
        } catch (e: IOException) {
            dest.delete()
            Result.failure(e)
        } finally {
            c.disconnect()
        }
    }

    /** Delete, send to the Slot17 team or take back: sending can take a minute, so a longer wait. */
    suspend fun supportAction(action: String, json: JsonObject): Result<ByteArray> =
        perform(open("api/support/$action", "POST", 150), json.toString().encodeToByteArray())

    companion object {
        fun status(error: Throwable?): Int? = (error as? HttpStatus)?.code
    }
}
