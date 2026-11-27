package nl.slot17.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.addPathNodes
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import nl.slot17.app.ActionState
import nl.slot17.app.DisplayStyle
import nl.slot17.core.Level
import nl.slot17.core.MeterColour

/** The iPhone app's colours (ios/Slot17/UI/Theme.swift), so both apps look like one product. */
/** The helicopter mark for an aircraft's channel (1.8.0), from core's Aircraft.GLYPH: the iPhone and the web page draw
 *  the same path. White, so an Icon's tint colours it. */
val AircraftIcon: ImageVector by lazy {
    ImageVector.Builder("aircraft", 24.dp, 24.dp, 24f, 24f)
        .addPath(addPathNodes(nl.slot17.core.Aircraft.GLYPH), fill = SolidColor(Color.White))
        .build()
}

object C {
    val background = Color(0xFF0B0E13)
    val panel = Color(0xFF131821)
    val raised = Color(0xFF1B222D)
    val line = Color(0xFF232B38)
    val text = Color(0xFFE8EDF4)
    val dim = Color(0xFF99A6B5)
    val green = Color(0xFF2FD26B)
    val yellow = Color(0xFFF5C542)
    val red = Color(0xFFFF4D4F)
    val grey = Color(0xFF3A4452)
    val blue = Color(0xFF4EA3FF)
    val segmentOff = Color(0xFF1B222D)

    fun level(l: Level) = when (l) { Level.OK -> green; Level.WARN -> yellow; Level.BAD -> red; Level.IDLE -> dim }

    fun hex(value: String): Color? = runCatching { Color(android.graphics.Color.parseColor(value)) }.getOrNull()

    fun meter(m: MeterColour, style: DisplayStyle) = when (m) {
        MeterColour.WEAK -> hex(style.weak) ?: Color(0xFF6F8BA8)
        MeterColour.YELLOW -> hex(style.silent) ?: yellow
        MeterColour.RED -> hex(style.chime) ?: red
        MeterColour.GREY -> grey
    }
}

val Slot17Colors = darkColorScheme(
    primary = C.green, onPrimary = Color.Black, secondary = C.blue, background = C.background, onBackground = C.text,
    surface = C.background, onSurface = C.text, surfaceVariant = C.panel, onSurfaceVariant = C.dim,
    surfaceContainer = C.panel, surfaceContainerHigh = C.raised, outline = C.line, error = C.red,
)

@Composable
fun ScreenTitle(text: String) {
    Text(text, color = C.text, fontSize = 34.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(top = 8.dp, bottom = 4.dp))
}

@Composable
fun Card(title: String?, content: @Composable ColumnScope.() -> Unit) {
    Column(
        Modifier.fillMaxWidth().background(C.panel, RoundedCornerShape(16.dp)).border(1.dp, C.line, RoundedCornerShape(16.dp)).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        if (title != null) Text(title.uppercase(), color = C.dim, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 0.5.sp)
        content()
    }
}

@Composable
fun Hint(text: String, color: Color = C.dim) = Text(text, color = color, fontSize = 12.sp, lineHeight = 16.sp)

@Composable
fun Rule() = HorizontalDivider(color = C.line)

@Composable
fun LabelledRow(name: String, value: String) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(name, color = C.dim, fontSize = 15.sp, modifier = Modifier.weight(1f, fill = false).padding(end = 12.dp))
        Text(value, color = C.text, fontSize = 15.sp, maxLines = 3, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
fun StatusDot(level: Level, size: Int) = Box(Modifier.size(size.dp).background(C.level(level), CircleShape))

@Composable
fun ActionButton(
    text: String,
    onClick: () -> Unit,
    tint: Color = C.raised,
    foreground: Color = C.text,
    icon: ImageVector? = null,
    enabled: Boolean = true,
    modifier: Modifier = Modifier.fillMaxWidth(),
) {
    Button(
        onClick = onClick, enabled = enabled, modifier = modifier.heightIn(min = 48.dp), shape = RoundedCornerShape(12.dp),
        colors = ButtonDefaults.buttonColors(containerColor = tint, contentColor = foreground,
            disabledContainerColor = C.raised.copy(alpha = 0.5f), disabledContentColor = C.dim),
    ) {
        if (icon != null) { Icon(icon, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp)) }
        Text(text, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
fun ActionNote(state: ActionState) {
    if (state.text.isEmpty()) return
    Text(state.text, color = when (state.level) { Level.OK -> C.green; Level.BAD -> C.red; else -> C.dim }, fontSize = 13.sp)
}

@Composable
fun ToggleRow(title: String, checked: Boolean, enabled: Boolean = true, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(title, color = if (enabled) C.text else C.dim, fontSize = 16.sp, modifier = Modifier.weight(1f).padding(end = 12.dp))
        Switch(checked = checked, onCheckedChange = onChange, enabled = enabled,
            colors = SwitchDefaults.colors(checkedTrackColor = C.green, checkedThumbColor = Color.White))
    }
}

@Composable
fun LevelSlider(title: String, value: Double, range: ClosedFloatingPointRange<Float>, unit: String = "dB", onChange: (Double) -> Unit) {
    Column {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(title, color = C.text, fontSize = 16.sp)
            Text("${value.toInt()} $unit", color = C.dim, fontSize = 15.sp)
        }
        val steps = (range.endInclusive - range.start).toInt() - 1
        Slider(value = value.toFloat(), onValueChange = { onChange(kotlin.math.round(it).toDouble()) }, valueRange = range,
            steps = steps.coerceAtLeast(0), colors = SliderDefaults.colors(activeTrackColor = C.green, thumbColor = Color.White))
    }
}

@Composable
fun <T> Segmented(options: List<Pair<T, String>>, selected: T, onSelect: (T) -> Unit) {
    Row(Modifier.fillMaxWidth().background(C.raised, RoundedCornerShape(12.dp)).padding(3.dp),
        horizontalArrangement = Arrangement.spacedBy(3.dp)) {
        for ((value, label) in options) {
            val on = value == selected
            Box(Modifier.weight(1f).heightIn(min = 38.dp)
                .background(if (on) C.grey else Color.Transparent, RoundedCornerShape(10.dp))
                .clickableNoRipple { onSelect(value) }, contentAlignment = Alignment.Center) {
                Text(label, color = if (on) C.text else C.dim, fontSize = 14.sp, fontWeight = if (on) FontWeight.SemiBold else FontWeight.Normal,
                    maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(horizontal = 6.dp))
            }
        }
    }
}

@Composable
fun Modifier.clickableNoRipple(onClick: () -> Unit): Modifier {
    val source = remember { MutableInteractionSource() }
    return this.clickable(interactionSource = source, indication = null, onClick = onClick)
}

/** Typography tuned for a phone held at arm's length in a car. */
@Composable
fun Slot17Theme(content: @Composable () -> Unit) = MaterialTheme(colorScheme = Slot17Colors, content = content)
