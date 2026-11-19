package nl.slot17.core

import java.math.BigDecimal
import java.math.RoundingMode
import kotlin.math.abs
import kotlin.math.floor

/**
 * Number formatting that gives exactly the text the iOS app shows.
 *
 * Swift's String(format: "%.1f") rounds the double's exact binary value, ties to even, always with a
 * dot. Java's String.format rounds HALF_UP and follows the phone's language, so a Dutch phone would
 * write "22,5". Both differences would make the two apps disagree on the same number.
 */
object Fmt {
    fun fixed(value: Double, decimals: Int): String {
        if (value.isNaN()) return "nan"
        if (value.isInfinite()) return if (value > 0) "inf" else "-inf"
        val text = BigDecimal(value).setScale(decimals, RoundingMode.HALF_EVEN).toPlainString()
        // C prints "-0.0" for a negative value that rounds to zero; keep that too.
        return if (value < 0 && !text.startsWith("-")) "-$text" else text
    }

    /** Swift's `x.rounded()`: to nearest, halves away from zero. Kotlin's round() would go to even. */
    fun roundAway(value: Double): Double = if (value < 0) -floor(abs(value) + 0.5) else floor(value + 0.5)
}
