package nl.slot17.app

import android.content.Context
import android.net.Uri
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.FileProvider
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import nl.slot17.core.DriveRow
import nl.slot17.core.Level
import nl.slot17.core.OpStatus
import nl.slot17.core.PiState
import nl.slot17.core.ProtocolJson
import nl.slot17.core.SupportPackage
import nl.slot17.core.SupportWords

/**
 * Support packages from the app (1.5.0): the car computer makes one (p0/support.py) and uploads it to the server;
 * here it is listed, shared through Android's share sheet, sent to the Slot17 team with the person's consent, taken
 * back from them, or deleted. [phoneFiles] uploads this phone's log and, only when asked, its drive data with places,
 * before the car computer is asked, so the server can add them to the package.
 */
class SupportRoute(
    private val context: Context,
    private val scope: CoroutineScope,
    private val client: ServerClient,
    private val sendOp: (String, JsonObject, (ActionState) -> Unit) -> Unit,
    private val carState: () -> PiState?,
    private val phoneFiles: (phoneLog: Boolean, places: Boolean, then: () -> Unit) -> Unit,
    private val note: (String) -> Unit,
) {
    var packages by mutableStateOf(emptyList<SupportPackage>())
        private set
    /** Asking the car computer for a package. */
    var request by mutableStateOf(ActionState())
        private set
    /** Sharing, sending, taking back or deleting one. */
    var fileAction by mutableStateOf(ActionState())
        private set
    /** A downloaded package for the share sheet; the screen shows it and clears this. */
    var share by mutableStateOf<Uri?>(null)

    val drives: List<DriveRow> get() = carState()?.drives.orEmpty()
    val progress: String? get() = SupportWords.progress(carState()?.support)

    val unavailable: String?
        get() {
            // Made on the car computer and kept on the server: without one (1.8.0) there is nowhere to put it.
            if (client.link == null) return "Support packages go through a server, and none is set up. On the car computer: python3 ~/Slot17/p0/support.py"
            val state = carState() ?: return "The car computer has not reported yet"
            if ("support" !in state.features.orEmpty()) return "The car computer needs software 1.5.0 for this"
            return null
        }

    fun create(choice: String, session: String?, window: List<Int>?, recordings: Boolean, phoneLog: Boolean, places: Boolean) {
        if (unavailable != null || request.busy) return
        val fields = mutableMapOf<String, kotlinx.serialization.json.JsonElement>(
            "choice" to JsonPrimitive(choice), "phone_log" to JsonPrimitive(phoneLog), "places" to JsonPrimitive(places),
        )
        if (choice != "quick" && session != null) fields["session"] = JsonPrimitive(session)
        if (choice != "quick" && window != null) fields["window"] = JsonArray(window.map { JsonPrimitive(it) })
        if (choice == "everything") fields["recordings"] = JsonPrimitive(recordings)
        note("support package requested: ${SupportWords.choice(choice)}${if (places) ", with places" else ""}")
        request = ActionState(text = "Preparing…", busy = true)
        phoneFiles(phoneLog, places) { sendOp("support", JsonObject(fields)) { request = it } }
    }

    /** The server's answer for the request: the car computer's ack, with the package's name when it is ready. */
    fun apply(op: OpStatus) {
        if (op.id != request.id) return
        request = when (op.state) {
            "done" -> ActionState(op.id, "Ready: ${op.detail}", Level.OK).also { refresh() }
            "failed", "expired" -> ActionState(op.id, op.detail.ifEmpty { "It did not work" }, Level.BAD)
            else -> ActionState(op.id, progress ?: "Waiting for the car computer…", Level.IDLE, true)
        }
    }

    fun refresh() {
        if (client.link == null) { packages = emptyList(); return }
        scope.launch {
            client.listSupport().onSuccess { packages = it }
                .onFailure { fileAction = ActionState(text = "Could not list them: ${it.message}", level = Level.BAD) }
        }
    }

    fun shareFile(p: SupportPackage) {
        fileAction = ActionState(text = "Downloading ${SupportWords.size(p.size)}…", busy = true)
        scope.launch {
            val dest = File(context.cacheDir, "support/${p.name}")
            context.cacheDir.resolve("support").listFiles()?.filter { it.name != p.name }?.forEach { it.delete() }
            client.downloadSupport(p.name, dest).onSuccess {
                share = FileProvider.getUriForFile(context, "${context.packageName}.files", it)
                fileAction = ActionState()
            }.onFailure { fileAction = ActionState(text = "Could not download it: ${it.message}", level = Level.BAD) }
        }
    }

    fun sendToTeam(p: SupportPackage) = action("send", p, "Sending to the Slot17 team…", consent = true) { body ->
        val code = runCatching { ProtocolJson.parseToJsonElement(body).jsonObject["code"]?.jsonPrimitive?.content }.getOrNull()
        note("support package sent to the Slot17 team as $code")
        "Sent. Your code is $code: give it to the Slot17 team. They delete it after 30 days."
    }

    fun takeBack(p: SupportPackage) = action("unsend", p, "Taking it back…") { "Deleted from the Slot17 team's inbox" }

    fun delete(p: SupportPackage) = action("delete", p, "Deleting…") { body ->
        when (runCatching { ProtocolJson.parseToJsonElement(body).jsonObject["team"]?.jsonPrimitive?.content }.getOrNull()) {
            "deleted" -> "Deleted from your server and from the Slot17 team's inbox"
            "left" -> "Deleted from your server. The Slot17 team's inbox did not answer; their copy goes after its 30 days."
            else -> "Deleted from your server"
        }
    }

    private fun action(kind: String, p: SupportPackage, busy: String, consent: Boolean = false, done: (String) -> String) {
        fileAction = ActionState(text = busy, busy = true)
        val fields = mutableMapOf<String, kotlinx.serialization.json.JsonElement>("name" to JsonPrimitive(p.name))
        if (consent) fields["consent"] = JsonPrimitive(true)
        scope.launch {
            client.supportAction(kind, JsonObject(fields)).onSuccess {
                fileAction = ActionState(text = done(it.decodeToString()), level = Level.OK)
                refresh()
            }.onFailure {
                val why = when (ServerClient.status(it)) {
                    400 -> "the server refused it (a package to send must be under 50 MB)"
                    502 -> "the Slot17 team's inbox did not answer; try again later"
                    else -> it.message
                }
                fileAction = ActionState(text = "It did not work: $why", level = Level.BAD)
            }
        }
    }
}
