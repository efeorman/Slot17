import ActivityKit
import Foundation

/// What the Live Activity shows on the Lock Screen, in the Dynamic Island and on the CarPlay
/// Dashboard. Shared by the app (which builds and sends it) and the widget extension (which draws
/// it). Kept small: ActivityKit allows 4 KB for attributes and state together.
struct LiveTileState: Codable, Hashable {
    enum Phase: String, Codable, Hashable {
        case listening      // car connected, detector running, nothing recent
        case signal         // a recent TETRA match on `channel`
        case linkLost       // nothing from the car for a while: silence means nothing
        case stopped        // the detector is not running
        case receiverQuiet  // the receiver hears nothing: antenna or cable?
        case ended          // the drive is over
    }

    /// The meter's meaning, exactly as in the app: never distance or vehicle type.
    enum Tone: String, Codable, Hashable { case none, weak, silent, chime, strong }

    struct Alert: Codable, Hashable {
        var urgent: Bool
        var channel: Int
        var strength: Int?
        var at: Date
    }

    struct Mark: Codable, Hashable {
        var title: String
        var at: Date
        /// The channel heard nearest the tap, as "L60 12 s before". Kept once shown.
        var context: String? = nil
    }

    /// A channel and when it was last heard.
    struct Heard: Codable, Hashable {
        var channel: Int
        var at: Date
    }

    struct Software: Codable, Hashable {
        var text: String
        var problem: Bool
    }

    var phase: Phase
    var channel: Int?
    /// Whole dB: tenths only cause updates nobody can read while driving.
    var strength: Int?
    /// 0...10 lit segments, on the app's own scale (full only at the urgent level).
    var level: Int
    var tone: Tone
    var frequent: Bool
    /// When the shown burst was heard; the tile counts up from it by itself.
    var heardAt: Date?
    var lastAlert: Alert?
    /// weak, silent, chime, outline: "#RRGGBB" from the server's settings.
    var colours: [String]
    var badge: String
    var demo: Bool
    /// Which burst `heardAt` belongs to (the Pi's event id), so a new burst a second later is not
    /// mistaken for clock jitter on the old one. Not shown.
    var burstID: String? = nil
    /// A sighting marked in the last moments, so a widget tap gets an answer on the Dashboard.
    var mark: Mark? = nil
    /// A short word about the car computer's software, for a few minutes after it changed.
    var software: Software? = nil
    /// Counts every update sent and is drawn small on the tile. A photo of the tile against the log
    /// then shows whether the screen really is behind what iOS accepted. Set when sending, so it never
    /// counts as a change itself.
    var rev: Int? = nil
    /// A call pattern on the shown channel: normal bursts frame after frame. Traffic, not identity.
    var call: Bool? = nil
    /// The shown channel is getting louder. Louder is not nearer.
    var rising: Bool? = nil
    /// Other channels heard in the last minute, besides the one shown.
    var others: Int? = nil
    /// While listening: the last channel shown and when, so a quiet tile still says something.
    var lastHeard: Heard? = nil
    /// The car's data is a few seconds late, not yet lost.
    var slow: Bool? = nil

    static let placeholder = LiveTileState(phase: .listening, channel: nil, strength: nil, level: 0, tone: .none,
                                           frequent: false, heardAt: nil, lastAlert: nil,
                                           colours: ["#6F8BA8", "#F5C542", "#FF4D4F", "#FFFFFF"],
                                           badge: "STRONG", demo: false)
}

struct SlotActivityAttributes: ActivityAttributes {
    typealias ContentState = LiveTileState
    var startedAt: Date
}
