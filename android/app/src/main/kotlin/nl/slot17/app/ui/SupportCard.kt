package nl.slot17.app.ui

import android.content.Intent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Inventory2
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Share
import androidx.compose.material.icons.automirrored.filled.Undo
import androidx.compose.material3.AlertDialog
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import nl.slot17.app.SupportRoute
import nl.slot17.core.SupportPackage
import nl.slot17.core.SupportWords

/** The Pi screen's support package card (1.5.0): make one, then share it, send it to the Slot17 team, or delete it. */
@Composable
fun SupportCard(support: SupportRoute) {
    val context = LocalContext.current
    var choice by remember { mutableStateOf("quick") }
    var session by remember { mutableStateOf<String?>(null) }
    var from by remember { mutableStateOf("") }
    var to by remember { mutableStateOf("") }
    var recordings by remember { mutableStateOf(false) }
    var phoneLog by remember { mutableStateOf(true) }
    var places by remember { mutableStateOf(false) }
    var askPlaces by remember { mutableStateOf(false) }
    var sending by remember { mutableStateOf<SupportPackage?>(null) }
    var deleting by remember { mutableStateOf<SupportPackage?>(null) }

    LaunchedEffect(Unit) { support.refresh() }
    support.share?.let { uri ->
        LaunchedEffect(uri) {
            val send = Intent(Intent.ACTION_SEND).setType("application/zip").putExtra(Intent.EXTRA_STREAM, uri)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            context.startActivity(Intent.createChooser(send, "Share the support package"))
            support.share = null
        }
    }

    Card("Support package") {
        Hint("One zip with the car computer's logs, its own check and what went wrong, for whoever helps you. Keys, passwords and your private link are left out.")
        Segmented(listOf("quick" to "Quick", "drive" to "One drive", "everything" to "Everything"), choice) { choice = it }
        Hint(when (choice) {
            "quick" -> "The last day's logs and the setup check. Small, usually under 1 MB."
            "drive" -> "One drive's matches, faults and logs."
            else -> "One drive with all its detail. Can be large."
        })
        if (choice != "quick") {
            val drives = support.drives
            if (drives.isEmpty()) Hint("The car computer has not listed its drives yet.")
            if (session == null || drives.none { it.session == session }) session = drives.firstOrNull()?.session
            for (d in drives.take(8)) {
                Row(Modifier.fillMaxWidth().clickableNoRipple { session = d.session }, verticalAlignment = Alignment.CenterVertically) {
                    RadioButton(selected = session == d.session, onClick = { session = d.session })
                    Text(SupportWords.drive(d), color = C.text, fontSize = 14.sp)
                }
            }
            Text("Only part of it (minutes after the start, optional)", color = C.dim, fontSize = 13.sp)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(from, { from = it.filter(Char::isDigit).take(4) }, Modifier.weight(1f), label = { Text("From") },
                    singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number))
                OutlinedTextField(to, { to = it.filter(Char::isDigit).take(4) }, Modifier.weight(1f), label = { Text("To") },
                    singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number))
            }
            if (choice == "everything") ToggleRow("Include the drive's recordings", recordings) { recordings = it }
        }
        ToggleRow("Include this phone's log", phoneLog) { phoneLog = it }
        ToggleRow("Include drive data with places", places) { if (it) askPlaces = true else places = false }
        if (places) Hint("Where you drove is in this package. Share it only with someone you trust.", C.red)
        val window = SupportWords.window(from, to)
        window.exceptionOrNull()?.let { Hint(it.message ?: "", C.red) }
        val reason = support.unavailable
        ActionButton("Create the package", {
            support.create(choice, session, window.getOrNull(), recordings, phoneLog, places)
        }, icon = Icons.Filled.Inventory2,
            enabled = reason == null && !support.request.busy && window.isSuccess && (choice == "quick" || session != null))
        ActionNote(support.request)
        reason?.let { Hint(it) }

        if (support.packages.isNotEmpty()) Rule()
        for (p in support.packages) PackageRow(p, support, busy = support.fileAction.busy,
            send = { sending = p }, delete = { deleting = p })
        ActionNote(support.fileAction)
        if (support.packages.isNotEmpty()) {
            Hint("Your server keeps the newest three. Share saves or sends the file with any app; the web page has a download too.")
        }
    }

    if (askPlaces) AlertDialog(onDismissRequest = { askPlaces = false }, containerColor = C.panel,
        title = { Text("Include where you drove?") },
        text = { Text("The drive data has every place this phone recorded on the drive, with times. Whoever gets the package can see where you were.") },
        confirmButton = { TextButton({ places = true; askPlaces = false }) { Text("Include places", color = C.red) } },
        dismissButton = { TextButton({ askPlaces = false }) { Text("Cancel") } })
    sending?.let { p ->
        AlertDialog(onDismissRequest = { sending = null }, containerColor = C.panel,
            title = { Text("Send to the Slot17 team?") },
            text = {
                Text("The package goes to the Slot17 team's inbox at support.slot17.cloud. Only the team can read it, and it is " +
                    "deleted after 30 days, or sooner when you take it back here. You get a code to tell them." +
                    if (p.phone.any { it.contains("places") }) " It includes where you drove." else "")
            },
            confirmButton = { TextButton({ support.sendToTeam(p); sending = null }) { Text("I agree, send it") } },
            dismissButton = { TextButton({ sending = null }) { Text("Cancel") } })
    }
    deleting?.let { p ->
        AlertDialog(onDismissRequest = { deleting = null }, containerColor = C.panel,
            title = { Text("Delete this package?") },
            text = { Text(if (p.teamCode != null) "It is deleted from your server and from the Slot17 team's inbox." else "It is deleted from your server.") },
            confirmButton = {
                TextButton({ support.delete(p); deleting = null }) { Text("Delete", color = C.red) }
            },
            dismissButton = { TextButton({ deleting = null }) { Text("Cancel") } })
    }
}

@Composable
private fun PackageRow(p: SupportPackage, support: SupportRoute, busy: Boolean, send: () -> Unit, delete: () -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.padding(vertical = 4.dp)) {
        Text("${SupportWords.choice(p.choice)}, ${SupportWords.size(p.size)}, ${nl.slot17.core.Format.duration(p.age)} ago",
            color = C.text, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        if (p.phone.isNotEmpty()) Hint("With this phone's ${p.phone.joinToString(" and ") { if (it.contains("places")) "drive data with places" else "log" }}")
        p.teamCode?.let { Text("Sent to the Slot17 team, code $it", color = C.green, fontSize = 14.sp) }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ActionButton("Share", { support.shareFile(p) }, icon = Icons.Filled.Share, enabled = !busy, modifier = Modifier.weight(1f))
            if (p.teamCode == null) {
                ActionButton("Send to team", send, icon = Icons.AutoMirrored.Filled.Send, enabled = !busy && p.size <= 50_000_000, modifier = Modifier.weight(1f))
            } else {
                ActionButton("Take back", { support.takeBack(p) }, icon = Icons.AutoMirrored.Filled.Undo, enabled = !busy, modifier = Modifier.weight(1f))
            }
            ActionButton("", delete, icon = Icons.Filled.Delete, enabled = !busy, modifier = Modifier.weight(0.5f))
        }
        if (p.teamCode == null && p.size > 50_000_000) Hint("Over 50 MB: too big for the Slot17 team's inbox. Share it instead, or make a smaller one.")
    }
}
