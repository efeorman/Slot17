package nl.slot17.core

/**
 * Urgent beeps as messages on an Android Auto screen, which Android Auto reads out. Android only:
 * on an iPhone the Live Activity shows on CarPlay instead.
 *
 * Android Auto wants a reply action on every message. A spoken reply here is a sighting, so "police"
 * or "saw nothing" marks what the driver saw without touching the phone.
 */
object CarMessages {
    /** At most one message per this many seconds, so Android Auto never reads out a string of them. */
    const val MIN_GAP_S = 30.0

    /** The last few messages Android Auto keeps in the conversation. */
    const val KEPT = 5

    fun text(channel: Int, strength: Double?): String =
        "Strong burst on L$channel" + (strength?.let { ", ${Fmt.fixed(it, 0)} dB" } ?: "")

    fun due(nowS: Double, lastS: Double?): Boolean = lastS == null || nowS - lastS >= MIN_GAP_S

    // English, Dutch and German, the languages of the countries Slot17 should work in first.
    private val WORDS: List<Pair<Sighting.Label, List<String>>> = listOf(
        Sighting.Label.NOTHING to listOf("nothing", "none", "niks", "niets", "nichts"),
        Sighting.Label.POLICE to listOf("police", "politie", "polizei"),
        Sighting.Label.AMBULANCE to listOf("ambulance", "ambu", "krankenwagen", "rettungswagen"),
        Sighting.Label.FIRE to listOf("fire", "brandweer", "feuerwehr"),
        Sighting.Label.UNKNOWN to listOf("other", "anders", "andere", "something"),
    )

    /** What a spoken reply means: "police", "I saw an ambulance", "niks"... or null if it's none of them. */
    fun label(reply: String): Sighting.Label? {
        val words = reply.lowercase().split(Regex("[^\\p{L}]+")).filter { it.isNotEmpty() }.toSet()
        return WORDS.firstOrNull { (_, keys) -> keys.any { it in words } }?.first
    }

    /** What the app answers in the conversation after a reply. */
    fun answer(label: Sighting.Label?): String =
        if (label != null) "Marked: ${label.title}" else "Not understood. Say police, ambulance, fire or nothing."
}
