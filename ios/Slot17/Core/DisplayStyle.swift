import Foundation

/// The meters' colours and words, changeable from the server's settings file.
/// Colours mean what the app does (weak / silent / chime / strong), never distance or vehicle type.
struct DisplayStyle: Equatable {
    var weak = "#6F8BA8"          // heard, below the yellow level: dim grey-blue, never green
    var silent = "#F5C542"        // yellow: heard, silent
    var chime = "#FF4D4F"         // red: chime level
    var strongOutline = "#FFFFFF" // outline at the urgent level
    var strongBadge = "STRONG"
    var frequentText = "Frequent bursts"
    var pulseOnNewStrong = true
    /// Seconds between ordinary tile updates; new beeps, problems and STRONG never wait.
    var tileGap: Double = 10
    /// This receiver's noise floor with nothing to hear, if the server's settings give one.
    var floorQuiet: Double? = nil

    mutating func apply(_ display: RemoteConfig.Display?) {
        self = DisplayStyle()
        guard let d = display else { return }
        if let v = d.weakColour { weak = v }
        if let v = d.silentColour { silent = v }
        if let v = d.chimeColour { chime = v }
        if let v = d.strongOutlineColour { strongOutline = v }
        if let v = d.strongBadge { strongBadge = v }
        if let v = d.frequentText { frequentText = v }
        if let v = d.pulseOnNewStrong { pulseOnNewStrong = v }
        if let v = d.tileGapS { tileGap = v }
        floorQuiet = d.floorQuietDB
    }
}

enum HexColour {
    /// The app's background, which every meter colour must stand out against.
    static let background = "#0B0E13"

    /// (r, g, b) in 0...1 for "#RRGGBB", or nil.
    static func rgb(_ hex: String) -> (Double, Double, Double)? {
        guard hex.count == 7, hex.first == "#", let value = UInt32(hex.dropFirst(), radix: 16) else { return nil }
        return (Double((value >> 16) & 0xFF) / 255, Double((value >> 8) & 0xFF) / 255, Double(value & 0xFF) / 255)
    }

    /// WCAG contrast ratio between two colours, 1...21.
    static func contrast(_ a: String, _ b: String) -> Double? {
        guard let x = rgb(a), let y = rgb(b) else { return nil }
        func lum(_ c: (Double, Double, Double)) -> Double {
            func ch(_ v: Double) -> Double { v <= 0.03928 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4) }
            return 0.2126 * ch(c.0) + 0.7152 * ch(c.1) + 0.0722 * ch(c.2)
        }
        let (l1, l2) = (lum(x), lum(y))
        return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)
    }

    /// Roughly green: the hue range people read as "safe", which a signal meter must not suggest.
    static func isGreenish(_ hex: String) -> Bool {
        guard let (r, g, b) = rgb(hex) else { return false }
        return g > r * 1.25 && g > b * 1.15 && g > 0.35
    }
}
