package nl.slot17.app.ui

import android.annotation.SuppressLint
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ContentPaste
import androidx.compose.material.icons.filled.Public
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlin.math.max
import nl.slot17.app.AppModel
import nl.slot17.core.BeepStyle
import nl.slot17.core.Fmt
import nl.slot17.core.MeterColour
import nl.slot17.core.SoundKind

const val GITHUB = "https://github.com/efeorman/Slot17"
const val DISCORD = "https://discord.gg/rSFPWSWNfh"
const val WEBSITE = "https://slot17.cloud"

fun open(context: Context, url: String) {
    runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
}

@Composable
fun SettingsScreen(model: AppModel, editLink: () -> Unit) {
    val context = LocalContext.current
    val s = model.settings
    var confirmRemove by remember { mutableStateOf(false) }
    Screen {
        ScreenTitle("Settings")
        Card("Connection") {
            LabelledRow("Server", model.linkHost.ifEmpty { "Not set" })
            ActionButton(if (model.hasLink) "Change the private link" else "Add your private link", editLink,
                tint = if (model.hasLink) C.raised else C.blue, foreground = if (model.hasLink) C.text else Color.Black)
            if (model.hasLink) ActionButton("Remove the link from this phone", { confirmRemove = true }, foreground = C.red)
            Hint(if (!model.hasLink || model.linkIsHttps) "The link works with your own server or a hosted one. An https link is encrypted all the way."
                else "This server uses plain HTTP, so the link and everything sent to it, including power off, is not encrypted.")
        }
        Card("Quiet mode") {
            ToggleRow("Quiet mode", s.quiet) { v -> model.updateSettings { it.copy(quiet = v) } }
            Hint("For busy roads. Chimes only from ${Fmt.fixed(max(s.red, s.quietRed), 0)} dB and at most one per " +
                "${Fmt.fixed(max(s.anyGap, s.quietGap), 0)} s. Urgent beeps (${Fmt.fixed(s.urgentLevel, 0)} dB and up) stay exactly the same. It may miss or delay a useful chime.")
        }
        Card("Sound") {
            ToggleRow("Sound", s.sound) { v -> model.updateSettings { it.copy(sound = v) } }
            ToggleRow("Urgent beep from ${Fmt.fixed(s.urgentLevel, 0)} dB", s.urgent) { v -> model.updateSettings { it.copy(urgent = v) } }
            Hint("Strong signal only. Not police, not a distance, and a quiet radio gives no beep.")
            Column {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text("Volume", color = C.text, fontSize = 16.sp)
                    Text("${(s.volume * 100).toInt()}%", color = C.dim, fontSize = 15.sp)
                }
                androidx.compose.material3.Slider(s.volume.toFloat(), { v -> model.updateSettings { it.copy(volume = (kotlin.math.round(v * 20) / 20.0).coerceIn(0.1, 1.0)) } },
                    valueRange = 0.1f..1f, colors = androidx.compose.material3.SliderDefaults.colors(activeTrackColor = C.green, thumbColor = Color.White))
            }
            Text("With music playing", color = C.text, fontSize = 16.sp)
            Segmented(BeepStyle.entries.map { it to it.title }, s.beepStyle) { v -> model.updateSettings { it.copy(beepStyle = v) } }
            Hint(when (s.beepStyle) {
                BeepStyle.MIX -> "The beep plays on top of the music. Nothing is turned down."
                BeepStyle.DUCK -> "The music dips for the beep, which adds about a quarter of a second."
                BeepStyle.PROMPT -> "Like a navigation voice. The music dips and spoken audio may pause. Some cars handle this differently."
            })
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ActionButton("Test beep", { model.test(SoundKind.URGENT) }, modifier = Modifier.weight(1f))
                ActionButton("Test chime", { model.test(SoundKind.CHIME) }, modifier = Modifier.weight(1f))
                ActionButton("Warning", { model.test(SoundKind.LINK_LOST) }, modifier = Modifier.weight(1f))
            }
            Text(model.soundLine, color = if (model.soundOK) C.dim else C.red, fontSize = 13.sp)
            Hint("Sounds use the media volume and follow the phone's audio to the car, like a navigation app.")
        }
        Card("Levels") {
            LevelSlider("Red (sound) from", s.red, 10f..45f) { v -> model.updateSettings { it.copy(red = v) } }
            LevelSlider("Yellow from", s.yellow, 6f..40f) { v -> model.updateSettings { it.copy(yellow = v) } }
            ToggleRow("Repeat needed", s.repeatNeeded) { v -> model.updateSettings { it.copy(repeatNeeded = v) } }
            Hint("Chime only if a channel matched twice in a minute. Fewer chimes, but a vehicle that transmits once stays silent. It is not a distance test.")
            Rule()
            MeterLegend(model)
        }
        Card("Behaviour") {
            ToggleRow("Warn when the link is lost", s.linkLostTone) { v -> model.updateSettings { it.copy(linkLostTone = v) } }
            Hint("One tone after ${Fmt.fixed(s.linkLostAfter, 0)} seconds without the car, so silence is not mistaken for nothing nearby.")
            ToggleRow("Warn when the receiver hears nothing", s.antennaWarning) { v -> model.updateSettings { it.copy(antennaWarning = v) } }
            ToggleRow("Keep the screen on in the dashboard", s.keepScreenOn) { v -> model.updateSettings { it.copy(keepScreenOn = v) } }
            Text("Sleep when closed and no car data", color = C.text, fontSize = 16.sp)
            Segmented(listOf(15 to "15 min", 30 to "30 min", 120 to "2 h", 0 to "Never"), s.autoSleepMinutes) { v ->
                model.updateSettings { it.copy(autoSleepMinutes = v) }
            }
            Hint("Saves battery after parking. Once asleep, open the app to listen again.")
            ToggleRow("Demo mode (fake data)", model.demo) { model.switchDemo(it) }
        }
        Card("Running in the background") {
            LabelledRow("Listening service", if (model.serviceRunning) "Running" else "Stopped")
            LabelledRow("Battery optimisation", if (model.ignoringBatteryOptimisation) "Off for Slot17" else "On")
            if (!model.ignoringBatteryOptimisation) ActionButton("Let Slot17 run in the background", { askBatteryExemption(context) })
            Hint("Slot17 keeps a notice while it listens, so Android does not stop it with the screen off. Some phones (Samsung, Xiaomi, Huawei) also need battery optimisation off for Slot17.")
        }
        Card("Ongoing notice") {
            ToggleRow("Show the strongest channel and the last beep", s.liveActivity) { v -> model.updateSettings { it.copy(liveActivity = v) } }
            Hint("In the notification shade and on the lock screen, with Saw police, Saw nothing and Mute buttons. On Android 16 it can show as a Live Update.")
        }
        Card("Android Auto") {
            ToggleRow("Show urgent beeps on the car screen", s.carMessages) { v -> model.updateSettings { it.copy(carMessages = v) } }
            Hint("An urgent beep becomes a message from Slot17, which Android Auto shows and reads out, at most one every 30 seconds and only while Android Auto is connected. Reply police, ambulance, fire or nothing to mark what you saw.")
            Hint("Slot17 isn't from Google Play, so Android Auto only shows it with Unknown sources on: in the Android Auto settings on your phone, tap Version 10 times, then open Developer settings from the menu and tick Unknown sources.")
        }
        Card("Fault messages") {
            ToggleRow("Show faults as notifications from this app", s.faultNotices) { v -> model.updateSettings { it.copy(faultNotices = v) } }
            if (s.faultNotices) LabelledRow("Android permission", model.noticePermission)
            Hint("The faults Pushover sends: detector degraded, receiver hears nothing, detector stopped, and once when all is well again. Only while this app listens.")
            Rule()
            ToggleRow("Pushover messages from the car computer", model.pushoverShown,
                enabled = model.pushoverUnavailable == null && !model.pushoverAction.busy) { model.setPushover(it) }
            model.pushoverOnCar?.let { LabelledRow("On the car computer", if (it.configured == false) "Pushover not set up" else if (it.pushover) "On" else "Off") }
            ActionNote(model.pushoverAction)
            model.pushoverUnavailable?.let { Hint(it) }
            Hint("Off stops every Pushover message from the car, faults included; switch the notifications above on first so you still hear about faults. A change made while the car is off waits up to a day and applies at the next drive.")
        }
        Card("Location") {
            ToggleRow("Record where alerts and sightings happen", s.recordLocation) { v -> model.updateSettings { it.copy(recordLocation = v) } }
            LabelledRow("Status", model.locationStatus)
            Hint("Stays on this phone, and goes to your own server with sightings and drive data. Used only to learn which roads and distances the alerts come with. It never changes when a sound plays.")
        }
        Card("Settings from the server") {
            LabelledRow("In use", model.configStatus)
            if (s.overrides.isNotEmpty()) {
                LabelledRow("Your own changes", s.overrides.sorted().joinToString(", "))
                ActionButton("Use the server settings again", model::useServerSettings)
            }
            ActionButton("Check for new settings", model::fetchConfig, icon = Icons.Filled.Refresh, enabled = model.hasLink && !model.demo)
            Hint("Sounds, levels, timings and messages can be changed on the server without a new app. Anything you change here stays yours.")
        }
        Card("About") {
            LabelledRow("Version", AppModel.version)
            model.release?.let { r ->
                val newer = model.appUpdate
                if (newer != null) {
                    Text("App $newer is available", color = C.green, fontSize = 15.sp)
                    ActionButton("Download and install $newer", model::installUpdate, enabled = !model.demo)
                    model.updateState?.let { Hint(it) }
                    Hint("Android asks you to confirm. It installs over this app, so your settings stay.")
                } else r.android?.let { LabelledRow("Newest release", "app $it, software ${r.software}") }
                r.url?.let { url -> ActionButton("Open the release on GitHub", { open(context, url) }, icon = Icons.Filled.Public) }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ActionButton("Website", { open(context, WEBSITE) }, modifier = Modifier.weight(1f))
                ActionButton("GitHub", { open(context, GITHUB) }, modifier = Modifier.weight(1f))
                ActionButton("Discord", { open(context, DISCORD) }, modifier = Modifier.weight(1f))
            }
            Hint("This app reports that a TETRA radio transmitted. It cannot identify a radio or vehicle or measure distance, and a silent vehicle is invisible.")
        }
    }
    if (confirmRemove) AlertDialog(onDismissRequest = { confirmRemove = false }, containerColor = C.panel,
        title = { Text("Remove the private link?") },
        confirmButton = { TextButton({ model.removeLink(); confirmRemove = false }) { Text("Remove", color = C.red) } },
        dismissButton = { TextButton({ confirmRemove = false }) { Text("Cancel") } })
}

@SuppressLint("BatteryLife")
private fun askBatteryExemption(context: Context) {
    runCatching {
        context.startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${context.packageName}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }.onFailure {
        runCatching { context.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
    }
}

/** What the meter colours mean, from the current levels and the server's colours and words. */
@Composable
private fun MeterLegend(model: AppModel) {
    val s = model.settings
    val style = model.style
    Text("METER COLOURS", color = C.dim, fontSize = 12.sp)
    @Composable fun row(colour: Color, text: String, outline: Boolean = false) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            val box = Modifier.size(22.dp, 14.dp).background(colour, RoundedCornerShape(4.dp))
            Box(if (outline) box.border(2.dp, C.hex(style.strongOutline) ?: Color.White, RoundedCornerShape(4.dp)) else box)
            Text(text, color = C.text, fontSize = 14.sp, modifier = Modifier.padding(start = 10.dp))
        }
    }
    row(C.meter(MeterColour.WEAK, style), "Heard, weak (below ${Fmt.fixed(s.yellow, 0)} dB)")
    row(C.meter(MeterColour.YELLOW, style), "Heard, silent (${Fmt.fixed(s.yellow, 0)}–${Fmt.fixed(s.effectiveRed, 0)} dB)")
    row(C.meter(MeterColour.RED, style), "Chime level (${Fmt.fixed(s.effectiveRed, 0)} dB and up)")
    row(C.meter(MeterColour.RED, style), "${style.strongBadge}: urgent beep level (${Fmt.fixed(s.urgentLevel, 0)} dB and up)", outline = true)
    Hint("Colours show what the app does with a signal, not how far away it is or what kind of vehicle. The bar is full only at the urgent level.")
}

@Composable
fun LinkDialog(model: AppModel, close: () -> Unit) {
    val context = LocalContext.current
    var text by remember { mutableStateOf("") }
    var problem by remember { mutableStateOf("") }
    AlertDialog(onDismissRequest = close, containerColor = C.panel, title = { Text("Private link") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("Paste the private link you open the web dashboard with, from your own server or a hosted one. It stays on this phone.",
                    color = C.dim, fontSize = 14.sp)
                OutlinedTextField(text, { text = it }, placeholder = { Text("https://…/?key=…") }, minLines = 2,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, autoCorrectEnabled = false), modifier = Modifier.fillMaxWidth())
                ActionButton("Paste", {
                    val clip = context.getSystemService(ClipboardManager::class.java)?.primaryClip
                    text = clip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(context)?.toString() ?: text
                }, icon = Icons.Filled.ContentPaste)
                if (problem.isNotEmpty()) Text(problem, color = C.red, fontSize = 13.sp)
            }
        },
        confirmButton = { TextButton({ model.setLink(text)?.let { problem = it } ?: close() }) { Text("Save") } },
        dismissButton = { TextButton(close) { Text("Cancel") } })
}
