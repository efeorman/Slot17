package nl.slot17.app.ui

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AddCircle
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.FiberManualRecord
import androidx.compose.material.icons.filled.PowerSettingsNew
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Schedule
import androidx.compose.material.icons.filled.StopCircle
import androidx.compose.material.icons.filled.Upload
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import nl.slot17.app.AppModel
import nl.slot17.core.Fmt
import nl.slot17.core.Format
import nl.slot17.core.MarkSummary
import nl.slot17.core.Sighting
import nl.slot17.core.SignalWords
import androidx.compose.foundation.layout.Box
import nl.slot17.core.Level
import nl.slot17.core.SetupCheck
import nl.slot17.core.CheckRow

private val when_ = SimpleDateFormat("d MMM HH:mm:ss", Locale.UK)
private fun time(ms: Long) = when_.format(Date(ms))

@Composable
fun Screen(content: @Composable () -> Unit) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        content()
    }
}

// Sightings

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun SightingsScreen(model: AppModel) {
    var adding by remember { mutableStateOf(false) }
    var removing by remember { mutableStateOf<Sighting?>(null) }
    Screen {
        ScreenTitle("Sightings")
        Hint("What you actually saw. These are what can show later whether alerts really go with vehicles.")
        SummaryCard(model.markSummary, model.settings.summaryWindow)
        ActionButton("Add a sighting with a note", { adding = true }, tint = C.blue, foreground = Color.Black, icon = Icons.Filled.AddCircle)
        if (model.sightings.isEmpty()) Card(null) {
            Text("Nothing yet. Tap Police, Ambulance or Fire on the Live screen when you see one, also when nothing beeped, and Saw nothing after a beep with nothing in sight.",
                color = C.dim, fontSize = 14.sp)
        }
        for (s in model.sightings) {
            val (icon, colour) = when (s.phase) {
                Sighting.Phase.SAVED -> Icons.Filled.CheckCircle to C.green
                Sighting.Phase.FAILED -> Icons.Filled.Warning to C.red
                else -> Icons.Filled.Schedule to C.dim
            }
            Row(Modifier.fillMaxWidth().background(C.panel, RoundedCornerShape(14.dp)).border(1.dp, C.line, RoundedCornerShape(14.dp))
                .combinedClickable(onClick = {}, onLongClick = { removing = s }).padding(12.dp)) {
                Icon(icon, null, tint = colour, modifier = Modifier.size(24.dp))
                Column(Modifier.padding(start = 12.dp)) {
                    Row {
                        Text(if (s.isRecordingRequest) "Saved recording" else s.label.title, color = C.text, fontSize = 16.sp,
                            fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                        Text(time(s.seenAtMs), color = C.dim, fontSize = 12.sp)
                    }
                    if (s.note.isNotEmpty()) Text(s.note, color = C.text, fontSize = 14.sp)
                    Text(s.statusText, color = if (s.phase == Sighting.Phase.FAILED) C.red else C.dim, fontSize = 13.sp)
                }
            }
        }
        if (model.sightings.isNotEmpty()) Hint("Long-press a sighting to remove it from this phone.")
    }
    if (adding) AddSightingDialog(model) { adding = false }
    removing?.let { s ->
        AlertDialog(onDismissRequest = { removing = null }, containerColor = C.panel,
            title = { Text("Remove this sighting?") },
            text = { Text("${s.label.title}, ${time(s.seenAtMs)}. It is removed from this phone only.") },
            confirmButton = { TextButton({ model.deleteSighting(s.id); removing = null }) { Text("Remove", color = C.red) } },
            dismissButton = { TextButton({ removing = null }) { Text("Cancel") } })
    }
}

@Composable
private fun SummaryCard(s: MarkSummary, window: Double) {
    Card("What your marks show") {
        if (s.urgent.played + s.chime.played + s.vehiclesMarked == 0) {
            Hint("Nothing to show yet. Each sound counts towards your first mark in the ${(window / 60).toInt()} minutes after it.")
            return@Card
        }
        Row { for (h in listOf("", "Played", "Vehicle", "Nothing", "No mark")) Text(h, color = C.dim, fontSize = 12.sp, modifier = Modifier.weight(1f)) }
        Rule()
        for ((name, row, colour) in listOf(Triple("Urgent", s.urgent, C.red), Triple("Chime", s.chime, C.yellow))) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Row(Modifier.weight(1f), verticalAlignment = Alignment.CenterVertically) {
                    androidx.compose.foundation.layout.Box(Modifier.size(8.dp).background(colour, RoundedCornerShape(4.dp)))
                    Text(" $name", color = C.text, fontSize = 15.sp)
                }
                for (v in listOf(row.played, row.vehicleSeen, row.nothingSeen, row.unmarked)) Text("$v", color = C.text, fontSize = 15.sp, modifier = Modifier.weight(1f))
            }
        }
        LabelledRow("Vehicles you marked", "${s.vehiclesMarked}")
        LabelledRow("With no sound before them", "${s.vehiclesWithoutSound}")
        Hint("A vehicle with no sound before it is a possible miss. Nothing means you saw no vehicle, not that none was there.")
    }
}

@Composable
private fun AddSightingDialog(model: AppModel, close: () -> Unit) {
    var label by remember { mutableStateOf(Sighting.Label.POLICE) }
    var minutes by remember { mutableStateOf(0) }
    var note by remember { mutableStateOf("") }
    AlertDialog(onDismissRequest = close, containerColor = C.panel, title = { Text("Add a sighting") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("What", color = C.dim, fontSize = 13.sp)
                Column {
                    for (l in Sighting.Label.entries) Row(Modifier.fillMaxWidth().clickableNoRipple { label = l },
                        verticalAlignment = Alignment.CenterVertically) {
                        RadioButton(selected = label == l, onClick = { label = l })
                        Text(l.title, color = C.text, fontSize = 15.sp)
                    }
                }
                Text("When", color = C.dim, fontSize = 13.sp)
                Segmented(listOf(0 to "Now", 1 to "1 min", 5 to "5 min", 10 to "10 min", 30 to "30 min"), minutes) { minutes = it }
                OutlinedTextField(note, { note = it }, label = { Text("Note") },
                    placeholder = { Text("For example: blue lights, driving towards me") }, minLines = 2, modifier = Modifier.fillMaxWidth())
                Hint("A recording is saved only for a sighting marked within about 5 seconds. Later ones are kept as a note.")
            }
        },
        confirmButton = { TextButton({ model.mark(label, note, minutes * 60.0); close() }) { Text("Save") } },
        dismissButton = { TextButton(close) { Text("Cancel") } })
}

// Pi

@Composable
fun PiScreen(model: AppModel) {
    var confirmPowerOff by remember { mutableStateOf(false) }
    var confirmStop by remember { mutableStateOf(false) }
    Screen {
        ScreenTitle("Pi")
        Card("Setup check") {
            val phone = model.setupRows
            val car = model.carRows
            SetupCheck.first(phone + car)?.let { first ->
                Text("Start here: ${first.name}", color = C.text, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                first.fix?.let { Hint(it) }
                Rule()
            }
            for (row in phone) CheckLine(row)
            Rule()
            Text("Car computer, its own check", color = C.dim, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
            model.piState?.doctor?.let { Hint("Checked ${Format.duration(it.age)} ago") }
            for (row in car) CheckLine(row)
            ActionBlock(model, "doctor", model.doctorAction) {
                ActionButton("Check the car computer again", model::requestDoctor, icon = Icons.Filled.Refresh, enabled = it && !model.doctorAction.busy)
            }
        }
        Card("Connection") {
            ChainView(model)
            NetworkDetails(model)
        }
        Card("Health") {
            val state = model.piState
            if (state == null) {
                Text("No data from the Pi right now.", color = C.dim, fontSize = 15.sp)
                return@Card
            }
            val pi = state.pi
            val d = state.detector
            pi?.sessionSeconds?.let { LabelledRow("This session", "about ${Format.duration(it)}") }
            d.windows?.let { LabelledRow("Windows analysed", "$it") }
            d.hits?.let { LabelledRow("Candidates", "$it") }
            if (d.unchecked != null && d.partial != null) LabelledRow("Unchecked / partly checked", "${d.unchecked} / ${d.partial}")
            pi?.droppedSamples?.let { LabelledRow("Dropped samples", Fmt.fixed(it, 0)) }
            d.inputPeak?.let { LabelledRow("Input peak", "${Fmt.fixed(it, 0)} counts") }
            d.floorDB?.let { LabelledRow("Noise floor", Format.floor(it, model.style.floorQuiet)) }
            model.interferencePercent?.let { LabelledRow("Interference, last minute", "${Fmt.fixed(it, 0)}% of windows") }
            pi?.temperature?.let { LabelledRow("Temperature", "${Fmt.fixed(it, 1)} °C") }
            pi?.load?.let { LabelledRow("Load", Fmt.fixed(it, 2)) }
            pi?.diskFreeGB?.let { LabelledRow("Disk free", "${Fmt.fixed(it, 1)} GB") }
            pi?.memFreeMB?.let { LabelledRow("Memory free", "${Fmt.fixed(it, 0)} MB") }
            pi?.uptime?.let { LabelledRow("Pi running for", Format.duration(it)) }
            state.software?.let { LabelledRow("Software version", it) }
            model.piUpdate?.let {
                Text("Software $it is available", color = C.green, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                Hint("Run the installer again on the car computer (README, Updating). With signed updates from your own server it installs by itself.")
            }
            state.update?.takeIf { it.state != "off" }?.let { LabelledRow("Software", Format.software(it)) }
            d.reasons?.takeIf { it.isNotEmpty() }?.let { LabelledRow("Faults", it.joinToString(", ")) }
            if (pi == null) Hint("This Pi has the older publisher, so it sends less detail.")
            Hint("Input level is not calibrated and does not show that the antenna works. Session time counts analysed windows.")
        }
        Card("Actions") {
            ActionBlock(model, "mark", null) {
                ActionButton("Save a recording now", { model.mark(Sighting.Label.UNKNOWN, "saved from the Pi screen", recording = true) },
                    icon = Icons.Filled.FiberManualRecord, enabled = it)
            }
            Hint("Keeps 3 seconds before and after, on the Pi. At most one per 10 seconds.")
            Rule()
            ActionBlock(model, "logs", model.piLogs) {
                ActionButton("Collect the Pi's logs", model::requestPiLogs, icon = Icons.Filled.Download, enabled = it && !model.piLogs.busy)
            }
            Rule()
            val stopReason = if (model.piState == null) "The Pi is not sending data" else null
            ActionButton("Stop the detector", { confirmStop = true }, icon = Icons.Filled.StopCircle, enabled = stopReason == null && !model.demo)
            ActionNote(model.stopAction)
            stopReason?.let { Hint(it) }
            Rule()
            ActionBlock(model, "poweroff", model.powerOff) {
                ActionButton("Power off the Pi", { confirmPowerOff = true }, tint = C.red.copy(alpha = 0.85f), foreground = Color.White,
                    icon = Icons.Filled.PowerSettingsNew, enabled = it && !model.powerOff.busy)
            }
        }
        SupportCard(model.support)
        Card("Starting again") {
            Text("There is no start button, on purpose. The detector starts by itself when the Pi powers up. Repeatedly starting and stopping can jam this Airspy.",
                color = C.dim, fontSize = 14.sp)
        }
    }
    if (confirmPowerOff) Confirm("Power off the Pi?", "This ends the recording session. The Pi comes back only when the ignition goes off and on again.",
        "Power off", { model.requestPowerOff() }) { confirmPowerOff = false }
    if (confirmStop) Confirm("Stop the detector?", "This ends the recording session. It cannot be started from this phone, and after this the app cannot reach the Pi.",
        "Stop the detector", { model.requestStop() }) { confirmStop = false }
}

@Composable
private fun CheckLine(row: CheckRow) {
    Row(verticalAlignment = Alignment.Top, modifier = Modifier.padding(vertical = 3.dp)) {
        Box(Modifier.padding(top = 6.dp)) { StatusDot(row.level, 9) }
        Column(Modifier.padding(start = 10.dp)) {
            Text("${row.name}: ${row.text}", color = C.text, fontSize = 14.sp)
            row.fix?.takeIf { row.level != Level.OK }?.let { Hint(it) }
        }
    }
}

@Composable
private fun ActionBlock(model: AppModel, feature: String, state: nl.slot17.app.ActionState?, button: @Composable (Boolean) -> Unit) {
    val reason = model.unavailable(feature)
    button(reason == null)
    state?.let { ActionNote(it) }
    reason?.let { Hint(it) }
}

@Composable
private fun Confirm(title: String, text: String, action: String, ok: () -> Unit, close: () -> Unit) {
    AlertDialog(onDismissRequest = close, containerColor = C.panel, title = { Text(title) }, text = { Text(text) },
        confirmButton = { TextButton({ ok(); close() }) { Text(action, color = C.red) } },
        dismissButton = { TextButton(close) { Text("Cancel") } })
}

@Composable
private fun NetworkDetails(model: AppModel) {
    val link = model.lastMessage?.state?.link
    model.phoneRoundTripMS?.let { LabelledRow("Phone to server", "${Fmt.fixed(it, 0)} ms there and back") }
    link ?: return
    LabelledRow("Car computer uses", link.viaText)
    link.medianMS?.let { LabelledRow("Its reports take", "${Fmt.fixed(it, 0)} ms typically" + (link.slowestMS?.let { s -> ", ${Fmt.fixed(s, 0)} ms at worst" } ?: "")) }
    link.wifiDBm?.let { LabelledRow("Wi-Fi signal", "${Fmt.fixed(it, 0)} dBm, ${SignalWords.wifi(it)}") }
    link.stick?.let { st ->
        st.provider?.let { LabelledRow("4G provider", it + if (st.roaming == true) ", roaming" else "") }
        st.rsrp?.let { LabelledRow("4G signal", "${Fmt.fixed(it, 0)} dBm, ${SignalWords.rsrp(it)}") }
    }
    if ((link.failed ?: 0.0) > 0) LabelledRow("Reports that failed", Fmt.fixed(link.failed ?: 0.0, 0))
}

// Logs

@Composable
fun LogsScreen(model: AppModel) {
    var part by remember { mutableStateOf(0) }
    var onlySounds by remember { mutableStateOf(false) }
    var reading by remember { mutableStateOf<Pair<String, String>?>(null) }
    LaunchedEffect(part) { if (part == 2) model.refreshServerLogs() }
    Screen {
        ScreenTitle("Logs")
        Segmented(listOf(0 to "Alerts", 1 to "Diagnostics", 2 to "On server"), part) { part = it }
        when (part) {
            0 -> {
                ToggleRow("Only alerts that made a sound", onlySounds) { onlySounds = it }
                val rows = model.alerts.filter { !onlySounds || it.sound != null }.take(300)
                if (rows.isEmpty()) Hint("No alerts yet.")
                for (a in rows) Column(Modifier.fillMaxWidth().background(C.panel, RoundedCornerShape(12.dp)).padding(12.dp)) {
                    Row {
                        Text(AlertText.title(a), color = C.text, fontSize = 15.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                        Text(SimpleDateFormat("HH:mm:ss", Locale.UK).format(Date(a.atMs)), color = C.dim, fontSize = 12.sp)
                    }
                    if (a.reason.isNotEmpty()) Text(a.reason, color = C.dim, fontSize = 13.sp)
                }
                if (model.alerts.isNotEmpty()) ActionButton("Clear the alert history", model::clearAlertHistory)
            }
            1 -> {
                Card("This phone") { for ((k, v) in model.diagnostics) LabelledRow(k, v) }
                ActionButton("Send this log to the server", model::uploadAppLog, icon = Icons.Filled.Upload,
                    enabled = model.hasLink && !model.logUpload.busy)
                ActionNote(model.logUpload)
                ActionButton("Clear the log on this phone", model::clearLocalLog)
                Card("Recent") {
                    for (line in model.logLines.takeLast(80).reversed())
                        Text(line, color = C.dim, fontSize = 11.sp, fontFamily = FontFamily.Monospace)
                }
            }
            else -> {
                ActionButton("Send this drive's data", { model.uploadDriveData() }, icon = Icons.Filled.Upload,
                    enabled = model.hasLink && !model.driveUpload.busy)
                ActionNote(model.driveUpload)
                Hint("Alerts, sightings and places since the last send. It also goes by itself a few minutes after the car is gone.")
                ActionButton("Collect the Pi's logs", model::requestPiLogs, icon = Icons.Filled.Download,
                    enabled = model.unavailable("logs") == null && !model.piLogs.busy)
                ActionNote(model.piLogs)
                if (model.phonePiLogs.isNotEmpty()) Hint("From the Pi over Bluetooth, kept on this phone.")
                for (log in model.phonePiLogs) Row(Modifier.fillMaxWidth().background(C.panel, RoundedCornerShape(12.dp))
                    .clickableNoRipple { reading = "From the Pi, over Bluetooth" to model.readPhonePiLog(log) }.padding(12.dp)) {
                    Text("From the Pi, over Bluetooth", color = C.text, fontSize = 13.sp, modifier = Modifier.weight(1f))
                    Text("${log.size / 1024 + 1} KB · ${Format.ago((System.currentTimeMillis() - log.atMs) / 1000.0)}", color = C.dim, fontSize = 12.sp)
                }
                ActionButton("Refresh the list", model::refreshServerLogs, icon = Icons.Filled.Refresh, enabled = model.hasLink)
                if (model.serverLogsNote.isNotEmpty()) Hint(model.serverLogsNote)
                for (entry in model.serverLogs) Row(Modifier.fillMaxWidth().background(C.panel, RoundedCornerShape(12.dp))
                    .clickableNoRipple { model.fetchServerLog(entry.name) { reading = entry.name to it } }.padding(12.dp)) {
                    Text(entry.name, color = C.text, fontSize = 13.sp, fontFamily = FontFamily.Monospace, modifier = Modifier.weight(1f))
                    Text("${entry.size / 1024} KB · ${Format.ago(entry.ageSeconds)}", color = C.dim, fontSize = 12.sp)
                }
            }
        }
    }
    reading?.let { (name, text) ->
        AlertDialog(onDismissRequest = { reading = null }, containerColor = C.panel, title = { Text(name, fontSize = 14.sp) },
            text = {
                Column(Modifier.heightIn(max = 480.dp).verticalScroll(rememberScrollState())) {
                    Text(text.takeLast(60_000), color = C.text, fontSize = 11.sp, fontFamily = FontFamily.Monospace)
                }
            },
            confirmButton = { TextButton({ reading = null }) { Text("Close") } })
    }
}
