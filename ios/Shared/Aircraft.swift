import SwiftUI

/// The aircraft channels (1.8.0). ECC/DEC/(06)05 sets 384.800-385.000 MHz aside for emergency services' aircraft, and
/// 384.750-384.800 MHz as extension: channels 190-199 on Slot17's grid (380.0125 MHz + 25 kHz x channel). Above about
/// 150 m an aircraft's radio uses only these (ETSI TR 102 300-6), so a match there is an aircraft, almost always a
/// police or ambulance helicopter; a low or landing one uses the ground channels like a car. The same rule as the car
/// computer (p0/detect.py), Android (core Aircraft) and the web page.
enum Aircraft {
    static let channels = 190...199
    static func isAir(_ channel: Int?) -> Bool { channel.map { channels.contains($0) } ?? false }
    /// The tag on a meter or an alert.
    static let word = "Aircraft"
    /// What VoiceOver says.
    static let spoken = "aircraft channel, likely a police or ambulance helicopter"
    /// The app's accent blue, never a strength colour.
    static let colour = Color(red: 0.306, green: 0.639, blue: 1.0)
}

/// A helicopter from the side, on a 24-unit square: the same drawing as Android's ic_aircraft and the web page's,
/// whose path is M2.8 3h18.4a0.8 0.8 0 0 1 0 1.6H2.8a0.8 0.8 0 0 1 0-1.6zM11.2 4.6h1.6v3.2h-1.6zM3.4 11.9a6.5 4.7 0 1 1 13 0a6.5 4.7 0 1 1-13 0zM15.6 10.8h5.8v1.6h-5.8zM20.4 8.9a0.9 0.9 0 0 1 1.8 0v3.8a0.9 0.9 0 0 1-1.8 0zM6.8 16.2h1.4v2.8H6.8zM12.2 16.2h1.4v2.8h-1.4zM4.75 18.8h10.9a0.75 0.75 0 0 1 0 1.5H4.75a0.75 0.75 0 0 1 0-1.5z
struct HelicopterGlyph: Shape {
    func path(in rect: CGRect) -> Path {
        let k = min(rect.width, rect.height) / 24
        let dx = rect.midX - 12 * k, dy = rect.midY - 12 * k
        func r(_ x: Double, _ y: Double, _ w: Double, _ h: Double) -> CGRect {
            CGRect(x: dx + x * k, y: dy + y * k, width: w * k, height: h * k)
        }
        var p = Path()
        p.addRoundedRect(in: r(2, 3, 20, 1.6), cornerSize: CGSize(width: 0.8 * k, height: 0.8 * k))   // rotor
        p.addRect(r(11.2, 4.6, 1.6, 3.2))                                                              // mast
        p.addEllipse(in: r(3.4, 7.2, 13, 9.4))                                                         // cabin
        p.addRect(r(15.6, 10.8, 5.8, 1.6))                                                             // tail boom
        p.addRoundedRect(in: r(20.4, 8, 1.8, 5.6), cornerSize: CGSize(width: 0.9 * k, height: 0.9 * k)) // fin
        p.addRect(r(6.8, 16.2, 1.4, 2.8))                                                              // struts
        p.addRect(r(12.2, 16.2, 1.4, 2.8))
        p.addRoundedRect(in: r(4, 18.8, 12.4, 1.5), cornerSize: CGSize(width: 0.75 * k, height: 0.75 * k)) // skid
        return p
    }
}

/// The tag: the glyph and the word, in the accent blue. `compact` drops the word where room is short.
struct AircraftTag: View {
    var compact = false
    var body: some View {
        HStack(spacing: 4) {
            HelicopterGlyph().fill(Aircraft.colour, style: FillStyle()).frame(width: 15, height: 15)
            if !compact { Text(Aircraft.word).fontWeight(.semibold).foregroundColor(Aircraft.colour) }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Aircraft.spoken)
    }
}
