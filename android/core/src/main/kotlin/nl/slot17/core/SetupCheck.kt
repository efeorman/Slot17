package nl.slot17.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** The car computer's own `setup.sh doctor` verdicts, as [part, level, text] (car computer 1.3.1 and later). */
@Serializable
data class DoctorReport(val items: List<List<String>> = emptyList(), @SerialName("age_s") val age: Double = 0.0)

data class CheckRow(val name: String, val level: Level, val text: String, val fix: String? = null)

/**
 * "Check my setup": every link from this phone to the receiver, in plain words, with what to do when one
 * isn't right. The phone's side comes from what the app sees; the car computer's side is its own doctor.
 */
object SetupCheck {
    fun phone(hasLink: Boolean, chain: List<ChainItem>, notifications: Boolean, batteryExempt: Boolean, sound: Boolean): List<CheckRow> =
        buildList {
            if (!hasLink) {
                add(CheckRow("Private link", Level.BAD, "Not added",
                    "Settings, Add your private link. You get it from /server create in the Slot17 Discord, or from your own server's setup."))
                return@buildList
            }
            add(CheckRow("Private link", Level.OK, "Added"))
            for (item in chain) add(CheckRow(item.name, item.level, item.detail, if (item.level == Level.BAD || item.level == Level.WARN) fixFor(item.id) else null))
            add(if (sound) CheckRow("Sound", Level.OK, "On") else CheckRow("Sound", Level.WARN, "Off", "Settings, Sound."))
            add(if (notifications) CheckRow("Notifications", Level.OK, "Allowed")
                else CheckRow("Notifications", Level.WARN, "Not allowed", "Allow them in Android's settings for Slot17, so it keeps listening with the screen off."))
            add(if (batteryExempt) CheckRow("Background", Level.OK, "Allowed to run")
                else CheckRow("Background", Level.WARN, "Android may stop it", "Settings, Let Slot17 run in the background."))
        }

    fun fixFor(id: String): String = when (id) {
        "server" -> "Check that this phone is online. With your own server: is it running, and is port 5732 open in its firewall?"
        "pi" -> "Is the car computer powered, and on your phone's hotspot? Its server address and key must match your server."
        "detector" -> "If it was stopped, it starts again at the next power-up. If it has a fault, the car computer's own check below says which."
        "receiver" -> "Check the receiver's USB cable and the antenna cable. Unplugging the receiver for a moment can help when it hangs."
        else -> "See the car computer's own check below."
    }

    fun car(report: DoctorReport?): List<CheckRow> {
        if (report == null) return listOf(CheckRow("Car computer", Level.IDLE, "Not checked yet",
            "It checks itself about a minute and a half after it starts (car computer software 1.3.1 and later)."))
        return report.items.filter { it.size == 3 }.map { (name, level, text) ->
            CheckRow(name, when (level) { "ok" -> Level.OK; "PROBLEM" -> Level.BAD; else -> Level.WARN }, text)
        }
    }

    /** The first thing to fix: problems before warnings, the phone's side before the car's, since one causes the other. */
    fun first(rows: List<CheckRow>): CheckRow? = rows.firstOrNull { it.level == Level.BAD } ?: rows.firstOrNull { it.level == Level.WARN }
}
