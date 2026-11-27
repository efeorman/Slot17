package nl.slot17.core

/**
 * The aircraft channels (1.8.0). ECC/DEC/(06)05 sets 384.800-385.000 MHz aside for emergency services' aircraft, and
 * 384.750-384.800 MHz as extension: channels 190-199 on Slot17's grid (380.0125 MHz + 25 kHz x channel). Above about
 * 150 m an aircraft's radio uses only these (ETSI TR 102 300-6), so a match there is an aircraft, almost always a police
 * or ambulance helicopter; a low or landing one uses the ground channels like a car. The same rule as the car computer
 * (p0/detect.py), the iPhone (Shared/Aircraft.swift) and the web page.
 */
object Aircraft {
    val channels = 190..199
    fun isAir(channel: Int?): Boolean = channel != null && channel in channels
    /** The tag on a meter or an alert. */
    const val WORD = "Aircraft"
    /** What TalkBack says. */
    const val SPOKEN = "aircraft channel, likely a police or ambulance helicopter"
    /** A helicopter from the side on a 24-unit square: res/drawable/ic_aircraft.xml, iOS and the web page draw it too. */
    const val GLYPH = "M2.8 3h18.4a0.8 0.8 0 0 1 0 1.6H2.8a0.8 0.8 0 0 1 0-1.6zM11.2 4.6h1.6v3.2h-1.6zM3.4 11.9a6.5 4.7 0 1 1 13 0a6.5 4.7 0 1 1-13 0zM15.6 10.8h5.8v1.6h-5.8zM20.4 8.9a0.9 0.9 0 0 1 1.8 0v3.8a0.9 0.9 0 0 1-1.8 0zM6.8 16.2h1.4v2.8H6.8zM12.2 16.2h1.4v2.8h-1.4zM4.75 18.8h10.9a0.75 0.75 0 0 1 0 1.5H4.75a0.75 0.75 0 0 1 0-1.5z"
}
