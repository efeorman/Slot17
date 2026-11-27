package nl.slot17.app.ui

import android.content.res.Configuration
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.DarkMode
import androidx.compose.material.icons.filled.GraphicEq
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.LocalFireDepartment
import androidx.compose.material.icons.filled.LocalPolice
import androidx.compose.material.icons.filled.MedicalServices
import androidx.compose.material.icons.filled.NorthEast
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.PhoneInTalk
import androidx.compose.material.icons.filled.QuestionMark
import androidx.compose.material.icons.filled.Schedule
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material.icons.filled.VolumeOff
import androidx.compose.material.icons.filled.VolumeUp
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import nl.slot17.app.AppModel
import nl.slot17.core.Aircraft
import nl.slot17.core.AlertRecord
import nl.slot17.core.Fmt
import nl.slot17.core.Format
import nl.slot17.core.MeterRow
import nl.slot17.core.Meters
import nl.slot17.core.Sighting
import nl.slot17.core.SoundKind

@Composable
fun LiveScreen(model: AppModel, editLink: () -> Unit) {
    val landscape = LocalConfiguration.current.orientation == Configuration.ORIENTATION_LANDSCAPE
    if (landscape) { Dashboard(model); return }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        StatusHeader(model)
        if (!model.hasLink && !model.demo && !model.bluetooth.paired) SetupCard(model, editLink)
        Box(Modifier.fillMaxWidth().background(C.panel, RoundedCornerShape(14.dp)).border(1.dp, C.line, RoundedCornerShape(14.dp)).padding(12.dp)) {
            ChainView(model)
        }
        Banners(model)
        MeterRowView(model, Modifier.fillMaxWidth().height(300.dp))
        if (model.moreChannels > 0) MoreChannels(model)
        else if (model.showBars && model.meters.isEmpty()) Hint("No TETRA detected")
        LastAlertCard(model)
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("I SAW", color = C.dim, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                for (label in Sighting.Label.vehicles) MarkButton(model, label, Modifier.weight(1f))
            }
            MarkButton(model, Sighting.Label.NOTHING, Modifier.fillMaxWidth())
            LastSightingLine(model)
        }
        Text(model.soundLine, color = if (model.soundOK) C.dim else C.red, fontSize = 13.sp)
        Hint("Signal strength above channel noise. Not a distance, not a specific vehicle.")
    }
}

@Composable
fun StatusHeader(model: AppModel) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        StatusDot(model.headline.level, 16)
        Column(Modifier.weight(1f).padding(horizontal = 12.dp)) {
            Text(model.headline.title, color = C.text, fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Text(model.headline.detail, color = C.dim, fontSize = 13.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
        MuteButton(model)
    }
}

@Composable
fun MuteButton(model: AppModel) {
    val muted = model.mutedSeconds > 0
    Box(Modifier.size(48.dp).background(C.raised, RoundedCornerShape(12.dp)), contentAlignment = Alignment.Center) {
        IconButton(onClick = model::toggleMute, modifier = Modifier.semantics { contentDescription = if (muted) "Unmute" else "Mute for 30 seconds" }) {
            Icon(if (muted || !model.settings.sound) Icons.Filled.VolumeOff else Icons.Filled.VolumeUp, null,
                tint = if (muted) C.yellow else C.text)
        }
        if (muted) Text("${model.mutedSeconds}", color = C.yellow, fontSize = 10.sp, fontWeight = FontWeight.Bold,
            modifier = Modifier.align(Alignment.BottomEnd).padding(3.dp))
    }
}

/** Phone to receiver, each link with its own state, so a failure names where it is. */
@Composable
fun ChainView(model: AppModel, vertical: Boolean = false) {
    val items = @Composable {
        for (item in model.chain) {
            Column(if (vertical) Modifier else Modifier.width(80.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    StatusDot(item.level, 8)
                    Text(item.name, color = C.text, fontSize = 12.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(start = 5.dp))
                }
                Text(item.detail, color = C.dim, fontSize = 11.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
            }
        }
    }
    if (vertical) Column(verticalArrangement = Arrangement.spacedBy(10.dp)) { items() }
    else Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) { items() }
}

@Composable
fun SegmentMeter(model: AppModel, row: MeterRow?, grey: Boolean, modifier: Modifier) {
    val s = model.settings
    val style = model.style
    val lit = Meters.level(row?.strength, s.urgentLevel)
    val kind = Meters.colour(row?.strength, s)
    val colour = if (grey) C.grey else C.meter(kind, style)
    val strong = !grey && Meters.isStrong(row?.strength, s)
    val air = !grey && row?.air == true
    val outline = C.hex(style.strongOutline) ?: Color.White
    val shown by animateFloatAsState(lit.toFloat(), tween(280), label = "segments")
    Column(modifier.alpha((row?.let { Meters.opacity(it.age) } ?: 0.45).toFloat())
        .background(C.panel, RoundedCornerShape(16.dp))
        .border(if (strong) 3.dp else if (air) 2.dp else 1.dp,
            if (strong) outline else if (air) C.blue.copy(alpha = 0.85f) else C.line, RoundedCornerShape(16.dp))
        .padding(vertical = 10.dp, horizontal = 6.dp)
        .semantics { contentDescription = spoken(row, strong, grey, style.strongBadge, style.frequentText) },
        horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Box(Modifier.height(18.dp), contentAlignment = Alignment.Center) {
            if (strong) Text(style.strongBadge, color = Color.Black, fontSize = 10.sp, fontWeight = FontWeight.Black,
                modifier = Modifier.background(outline, CircleShape).padding(horizontal = 7.dp, vertical = 1.dp))
        }
        Canvas(Modifier.weight(1f).fillMaxWidth(0.8f)) {
            val gap = 4.dp.toPx()
            val h = ((size.height - gap * (Meters.SEGMENTS - 1)) / Meters.SEGMENTS).coerceAtLeast(3f)
            for (i in 0 until Meters.SEGMENTS) {
                val y = size.height - (i + 1) * h - i * gap
                val on = i < shown - 0.01f
                drawRoundRect(if (on) colour else C.segmentOff, Offset(0f, y), Size(size.width, h), CornerRadius(4.dp.toPx()))
            }
        }
        if (row != null) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                // An aircraft's channel (1.8.0): the helicopter mark before the number.
                if (air) Icon(AircraftIcon, null, tint = C.blue, modifier = Modifier.size(17.dp).padding(end = 3.dp))
                Text("L${row.ch}", color = C.text, fontSize = 17.sp, fontWeight = FontWeight.SemiBold, fontFamily = FontFamily.Monospace)
            }
            Text(row.strength?.let { "${Fmt.fixed(it, 1)} dB" } ?: "strength n/a", color = C.text, fontSize = 15.sp,
                fontWeight = FontWeight.SemiBold, maxLines = 1)
            Text("${Fmt.fixed(row.freqMHz, 4)} MHz", color = C.dim, fontSize = 11.sp, maxLines = 1)
            Text("${if (row.approxTime) "≈ " else ""}${Format.ago(row.age)} · ${row.hits60}×/min", color = C.dim, fontSize = 11.sp,
                maxLines = 1, textAlign = TextAlign.Center)
            if (air) Text(Aircraft.WORD.uppercase(), color = C.blue, fontSize = 10.sp, fontWeight = FontWeight.Black,
                letterSpacing = 0.6.sp, maxLines = 1)
            if (row.call && !grey) Tag(Icons.Filled.PhoneInTalk, "In a call")
            else if (row.frequent && !grey) Tag(Icons.Filled.GraphicEq, style.frequentText)
            if (row.rising && !grey) Tag(Icons.Filled.NorthEast, "Rising")
        } else {
            Text("—", color = C.dim, fontSize = 17.sp)
        }
    }
}

private fun spoken(row: MeterRow?, strong: Boolean, grey: Boolean, badge: String, frequent: String): String {
    row ?: return "No channel"
    val parts = mutableListOf("Channel L${row.ch}")
    if (row.air && !grey) parts.add(Aircraft.SPOKEN)
    if (strong) parts.add(badge)
    parts.add(row.strength?.let { "${Fmt.fixed(it, 0)} decibels above the noise" } ?: "strength not available")
    parts.add((if (row.approxTime) "about " else "") + Format.ago(row.age).replace(" s ago", " seconds ago"))
    parts.add("${row.hits60} a minute")
    if (row.call && !grey) parts.add("in a call") else if (row.frequent && !grey) parts.add(frequent)
    if (row.rising && !grey) parts.add("rising")
    return parts.joinToString(", ")
}

@Composable
private fun Tag(icon: ImageVector, text: String) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Icon(icon, null, tint = C.text, modifier = Modifier.size(12.dp))
        Text(text, color = C.text, fontSize = 10.sp, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(start = 3.dp))
    }
}

@Composable
fun MeterRowView(model: AppModel, modifier: Modifier) {
    Row(modifier, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        for (i in 0 until 3) SegmentMeter(model, model.meters.getOrNull(i), !model.showBars, Modifier.weight(1f).fillMaxHeight())
    }
}

@Composable
fun Banners(model: AppModel) {
    if (model.demo) Text("DEMO. NOT REAL DETECTIONS", color = Color.Black, fontSize = 13.sp, fontWeight = FontWeight.Bold,
        textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth().background(C.yellow, RoundedCornerShape(8.dp)).padding(6.dp))
    if (model.banner.isNotEmpty()) Row(Modifier.fillMaxWidth().background(C.blue, RoundedCornerShape(8.dp)).padding(8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Icon(Icons.Filled.Info, null, tint = Color.Black, modifier = Modifier.size(18.dp))
        Text(model.banner, color = Color.Black, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(start = 8.dp))
    }
    if (model.settings.quiet) Row(verticalAlignment = Alignment.CenterVertically) {
        Icon(Icons.Filled.DarkMode, null, tint = C.dim, modifier = Modifier.size(14.dp))
        Text("Quiet mode: chimes from ${Fmt.fixed(model.settings.effectiveRed, 0)} dB. Urgent beeps unchanged.", color = C.dim,
            fontSize = 12.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(start = 6.dp))
    }
    if (model.receiverQuiet) Text("Receiver hears nothing. Check the antenna and its cable.", color = Color.White, fontSize = 13.sp,
        fontWeight = FontWeight.Bold, textAlign = TextAlign.Center,
        modifier = Modifier.fillMaxWidth().background(C.red, RoundedCornerShape(8.dp)).padding(8.dp))
}

private fun icon(label: Sighting.Label): Pair<ImageVector, Color> = when (label) {
    Sighting.Label.POLICE -> Icons.Filled.LocalPolice to C.blue
    Sighting.Label.AMBULANCE -> Icons.Filled.MedicalServices to C.yellow
    Sighting.Label.FIRE -> Icons.Filled.LocalFireDepartment to C.red
    Sighting.Label.UNKNOWN -> Icons.Filled.QuestionMark to C.dim
    Sighting.Label.NOTHING -> Icons.Filled.VisibilityOff to C.dim
}

@Composable
fun MarkButton(model: AppModel, label: Sighting.Label, modifier: Modifier, compact: Boolean = false) {
    val haptic = LocalHapticFeedback.current
    val (symbol, tint) = icon(label)
    Box(modifier.heightIn(min = if (compact) 46.dp else if (label == Sighting.Label.NOTHING) 44.dp else 66.dp)
        .background(C.raised, RoundedCornerShape(14.dp))
        .clickableNoRipple { haptic.performHapticFeedback(HapticFeedbackType.LongPress); model.mark(label) }
        .semantics { contentDescription = "I saw: ${label.title}" }, contentAlignment = Alignment.Center) {
        if (label == Sighting.Label.NOTHING && !compact) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(symbol, null, tint = tint, modifier = Modifier.size(18.dp))
                Text("Saw nothing after the beep", color = C.text, fontSize = 13.sp, fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.padding(start = 8.dp))
            }
        } else {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Icon(symbol, null, tint = tint, modifier = Modifier.size(if (compact) 18.dp else 26.dp))
                Text(label.title, color = C.text, fontSize = if (compact) 10.sp else 12.sp, fontWeight = FontWeight.SemiBold, maxLines = 1)
            }
        }
    }
}

@Composable
fun LastSightingLine(model: AppModel) {
    val last = model.sightings.firstOrNull() ?: return
    if (model.nowMs - last.seenAtMs >= 120_000) return
    val (icon, colour) = when (last.phase) {
        Sighting.Phase.FAILED -> Icons.Filled.Warning to C.red
        Sighting.Phase.SAVED -> Icons.Filled.CheckCircle to C.green
        else -> Icons.Filled.Schedule to C.dim
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Icon(icon, null, tint = colour, modifier = Modifier.size(16.dp))
        Text("${last.label.title}: ${last.statusText}", color = C.dim, fontSize = 13.sp, modifier = Modifier.padding(start = 6.dp))
    }
}

object AlertText {
    fun title(a: AlertRecord): String {
        val db = AppModel.db(a.strength)
        // An aircraft's channel says so (1.8.0).
        val ch = if (Aircraft.isAir(a.channel)) "L${a.channel} · aircraft" else "L${a.channel}"
        return when (a.sound) {
            SoundKind.URGENT -> "Urgent beep · $ch · $db"
            SoundKind.CHIME -> "Chime · $ch · $db"
            SoundKind.CALL -> "Call tone · $ch · $db"
            else -> "Silent · $ch · $db"
        }
    }
}

@Composable
fun LastAlertCard(model: AppModel) {
    val last = model.alerts.firstOrNull { it.sound != null }
    Row(Modifier.fillMaxWidth().background(C.panel, RoundedCornerShape(12.dp)).border(1.dp, C.line, RoundedCornerShape(12.dp))
        .padding(horizontal = 12.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
        if (last != null && Aircraft.isAir(last.channel)) Icon(AircraftIcon, null, tint = C.blue, modifier = Modifier.size(24.dp))
        else Icon(Icons.Filled.Notifications, null, tint = if (last?.sound == SoundKind.URGENT) C.red else C.dim)
        Column(Modifier.padding(start = 10.dp)) {
            if (last != null) {
                Text(AlertText.title(last), color = C.text, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                Text(Format.ago((model.nowMs - last.atMs) / 1000.0), color = C.dim, fontSize = 12.sp)
            } else Text("No alert yet", color = C.dim, fontSize = 15.sp)
        }
    }
}

/** The channels beyond the three meters, as small chips. */
@Composable
fun MoreChannels(model: AppModel) {
    val hidden = model.moreChannels - model.extraMeters.size
    Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp),
        verticalAlignment = Alignment.CenterVertically) {
        for (row in model.extraMeters) {
            Row(Modifier.alpha(Meters.opacity(row.age).toFloat()).background(C.panel, CircleShape).border(1.dp, C.line, CircleShape)
                .padding(horizontal = 8.dp, vertical = 5.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(7.dp).background(C.meter(Meters.colour(row.strength, model.settings), model.style), CircleShape))
                if (row.air) Icon(AircraftIcon, null, tint = C.blue, modifier = Modifier.padding(start = 4.dp).size(13.dp))
                Text(" L${row.ch}", color = C.text, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                row.strength?.let { Text(" ${Fmt.fixed(it, 0)} dB", color = C.text, fontSize = 12.sp) }
                Text(" ${Format.ago(row.age)}", color = C.dim, fontSize = 11.sp)
            }
        }
        if (hidden > 0) Text("+$hidden", color = C.dim, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
fun SetupCard(model: AppModel, editLink: () -> Unit) {
    Card("Connect to your Pi") {
        Text("Add the private link for your display server to start receiving. Your own server or a hosted one, both work the same. No server? Settings, Bluetooth to the car computer, Without a server: the app then gets everything straight from the car computer.",
            color = C.dim, fontSize = 14.sp)
        ActionButton("Add your private link", editLink, tint = C.blue, foreground = Color.Black)
        ActionButton("Try the demo", { model.switchDemo(true) })
    }
}

/** Landscape: status left, meters in the middle, the I SAW buttons right. */
@Composable
fun Dashboard(model: AppModel) {
    Row(Modifier.fillMaxSize().background(C.background).padding(10.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Column(Modifier.width(180.dp).fillMaxHeight(), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                StatusDot(model.headline.level, 14)
                Column(Modifier.padding(start = 10.dp)) {
                    Text(model.headline.title, color = C.text, fontSize = 16.sp, fontWeight = FontWeight.SemiBold, maxLines = 1)
                    Text(model.headline.detail, color = C.dim, fontSize = 11.sp, maxLines = 2)
                }
            }
            Banners(model)
            ChainView(model, vertical = true)
            Box(Modifier.weight(1f))
            MuteButton(model)
        }
        Column(Modifier.weight(1f).fillMaxHeight(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            MeterRowView(model, Modifier.weight(1f).fillMaxWidth())
            Hint("Signal strength above channel noise. Not a distance, not a specific vehicle.")
        }
        Column(Modifier.width(96.dp).fillMaxHeight(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("I SAW", color = C.dim, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
            for (label in Sighting.Label.vehicles) MarkButton(model, label, Modifier.fillMaxWidth(), compact = true)
            MarkButton(model, Sighting.Label.NOTHING, Modifier.fillMaxWidth(), compact = true)
        }
    }
}

