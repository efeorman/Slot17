import Foundation

/// How one sound is made: notes on a waveform with a simple envelope, or a WAV file from the server.
struct SoundSpec: Codable, Equatable {
    var wave: String = "sine"            // sine, square, triangle or bell
    var peak: Double = 0.6
    var attackS: Double = 0.01
    var releaseS: Double = 0.1
    var fadeS: Double?                   // bell only: how fast it rings out
    var notes: [[Double]] = []           // [frequency Hz, start s, length s]
    var wav: String?                     // a file served from /api/sounds/ instead of notes

    enum CodingKeys: String, CodingKey {
        case wave, peak, notes, wav
        case attackS = "attack_s"
        case releaseS = "release_s"
        case fadeS = "fade_s"
    }

    /// Nil if usable, otherwise the reason. A bad sound must never reach the speaker.
    var problem: String? {
        if let wav {
            return wav.range(of: "^[a-z0-9_-]{1,40}\\.wav$", options: .regularExpression) == nil ? "bad file name" : nil
        }
        guard ["sine", "square", "triangle", "bell"].contains(wave) else { return "unknown wave \(wave)" }
        guard (0.05...1).contains(peak) else { return "peak out of range" }
        guard (0...0.5).contains(attackS), (0...3).contains(releaseS) else { return "envelope out of range" }
        if let fadeS, !(0.01...5).contains(fadeS) { return "fade out of range" }
        guard (1...16).contains(notes.count) else { return "needs 1 to 16 notes" }
        for note in notes {
            guard note.count == 3, (50...10_000).contains(note[0]), (0...3).contains(note[1]), (0.01...3).contains(note[2])
            else { return "a note is out of range" }
        }
        return totalSeconds > 4 ? "longer than 4 s" : nil
    }

    var totalSeconds: Double { notes.map { $0[1] + $0[2] }.max() ?? 0 }
}

extension SoundSpec {
    /// Missing fields keep their defaults, so `{"wav": "bell.wav"}` alone is a valid sound.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = SoundSpec()
        wave = try c.decodeIfPresent(String.self, forKey: .wave) ?? d.wave
        peak = try c.decodeIfPresent(Double.self, forKey: .peak) ?? d.peak
        attackS = try c.decodeIfPresent(Double.self, forKey: .attackS) ?? d.attackS
        releaseS = try c.decodeIfPresent(Double.self, forKey: .releaseS) ?? d.releaseS
        fadeS = try c.decodeIfPresent(Double.self, forKey: .fadeS)
        notes = try c.decodeIfPresent([[Double]].self, forKey: .notes) ?? d.notes
        wav = try c.decodeIfPresent(String.self, forKey: .wav)
    }
}

/// The settings file on the display server. Every field is optional; missing ones keep defaults.
struct RemoteConfig: Codable, Equatable {
    struct Rules: Codable, Equatable {
        var redDB, yellowDB, urgentDB, urgentGapS, chimeGapAnyS, chimeGapChannelS, maxAgeS, quietRedDB, quietGapS: Double?
        var callDB, callGapChannelS, callGapAnyS: Double?
        enum CodingKeys: String, CodingKey {
            case redDB = "red_db", yellowDB = "yellow_db", urgentDB = "urgent_db", urgentGapS = "urgent_gap_s"
            case chimeGapAnyS = "chime_gap_any_s", chimeGapChannelS = "chime_gap_channel_s", maxAgeS = "max_age_s"
            case quietRedDB = "quiet_red_db", quietGapS = "quiet_gap_s"
            case callDB = "call_db", callGapChannelS = "call_gap_channel_s", callGapAnyS = "call_gap_any_s"
        }
    }
    struct Warnings: Codable, Equatable {
        var linkLostAfterS: Double?
        var receiverQuietWindows: Int?
        var receiverToneRepeatS: Double?
        enum CodingKeys: String, CodingKey {
            case linkLostAfterS = "link_lost_after_s", receiverQuietWindows = "receiver_quiet_windows"
            case receiverToneRepeatS = "receiver_tone_repeat_s"
        }
    }
    struct Marks: Codable, Equatable {
        var summaryWindowS: Double?
        enum CodingKeys: String, CodingKey { case summaryWindowS = "summary_window_s" }
    }
    struct Location: Codable, Equatable {
        var trackEveryS: Double?
        enum CodingKeys: String, CodingKey { case trackEveryS = "track_every_s" }
    }
    /// Meter colours ("#RRGGBB") and words. See DisplayStyle for the built-in values.
    struct Display: Codable, Equatable {
        var weakColour, silentColour, chimeColour, strongOutlineColour: String?
        var strongBadge, frequentText: String?
        var pulseOnNewStrong: Bool?
        /// Seconds between ordinary tile updates, so it can be tried out without a new app.
        var tileGapS: Double?
        /// The noise floor of this receiver with nothing to hear, so the app can show how far above it the car is.
        var floorQuietDB: Double?
        enum CodingKeys: String, CodingKey {
            case floorQuietDB = "floor_quiet_db"
            case weakColour = "weak_colour", silentColour = "silent_colour", chimeColour = "chime_colour"
            case strongOutlineColour = "strong_outline_colour", strongBadge = "strong_badge"
            case frequentText = "frequent_text", pulseOnNewStrong = "pulse_on_new_strong", tileGapS = "tile_gap_s"
        }

        var problems: [String] {
            var out: [String] = []
            let colours = [("weak_colour", weakColour), ("silent_colour", silentColour), ("chime_colour", chimeColour),
                           ("strong_outline_colour", strongOutlineColour)]
            for (name, value) in colours {
                guard let value else { continue }
                guard let ratio = HexColour.contrast(value, HexColour.background) else {
                    out.append("\(name) is not #RRGGBB"); continue
                }
                if ratio < 3 { out.append("\(name) is too dark to read on the background") }
                if HexColour.isGreenish(value) { out.append("\(name) is green, which reads as safe") }
            }
            if let silent = silentColour, let chime = chimeColour, silent.lowercased() == chime.lowercased() {
                out.append("silent_colour and chime_colour are the same")
            }
            if let t = strongBadge, t.trimmingCharacters(in: .whitespaces).isEmpty || t.count > 10 {
                out.append("strong_badge must be 1-10 characters")
            }
            if let t = frequentText, t.count > 20 { out.append("frequent_text longer than 20 characters") }
            if let g = tileGapS, !(1...60).contains(g) { out.append("tile_gap_s out of range") }
            if let f = floorQuietDB, !(-300...300).contains(f) { out.append("floor_quiet_db out of range") }
            return out
        }
    }

    var schema: Int?
    var message: String?
    var rules: Rules?
    var warnings: Warnings?
    var marks: Marks?
    var location: Location?
    var display: Display?
    var sounds: [String: SoundSpec]?

    /// Every reason this file cannot be used. Empty means it can.
    var problems: [String] {
        var out: [String] = []
        func check(_ value: Double?, _ range: ClosedRange<Double>, _ name: String) {
            if let value, !range.contains(value) { out.append("\(name) out of range") }
        }
        if let schema, schema != 1 { out.append("unknown schema \(schema)") }
        if let message, message.count > 200 { out.append("message longer than 200 characters") }
        if let r = rules {
            check(r.redDB, 5...80, "red_db"); check(r.yellowDB, 0...80, "yellow_db"); check(r.urgentDB, 10...90, "urgent_db")
            check(r.urgentGapS, 0...60, "urgent_gap_s"); check(r.chimeGapAnyS, 0...600, "chime_gap_any_s")
            check(r.chimeGapChannelS, 0...600, "chime_gap_channel_s"); check(r.maxAgeS, 2...60, "max_age_s")
            check(r.quietRedDB, 5...90, "quiet_red_db"); check(r.quietGapS, 0...600, "quiet_gap_s")
            // 99 switches the call tone off from the server.
            check(r.callDB, 5...99, "call_db"); check(r.callGapChannelS, 0...3600, "call_gap_channel_s")
            check(r.callGapAnyS, 0...600, "call_gap_any_s")
            if let red = r.redDB, let yellow = r.yellowDB, yellow > red { out.append("yellow_db above red_db") }
        }
        if let w = warnings {
            check(w.linkLostAfterS, 5...600, "link_lost_after_s"); check(w.receiverToneRepeatS, 30...7200, "receiver_tone_repeat_s")
            if let q = w.receiverQuietWindows, !(3...1000).contains(q) { out.append("receiver_quiet_windows out of range") }
        }
        check(marks?.summaryWindowS, 10...600, "summary_window_s")
        check(location?.trackEveryS, 2...600, "track_every_s")
        out += display?.problems ?? []
        for (name, spec) in sounds ?? [:] {
            if SoundKind(configName: name) == nil { out.append("unknown sound \(name)") }
            if let p = spec.problem { out.append("sound \(name): \(p)") }
        }
        return out
    }

    static func decode(_ data: Data) -> (version: String, config: RemoteConfig)? {
        struct Envelope: Decodable { let version: String; let config: RemoteConfig }
        guard let envelope = try? JSONDecoder().decode(Envelope.self, from: data) else { return nil }
        return (envelope.version, envelope.config)
    }
}

extension SoundKind {
    init?(configName: String) {
        switch configName {
        case "urgent": self = .urgent
        case "chime": self = .chime
        case "call": self = .call
        case "warning": self = .warning
        case "link_lost": self = .linkLost
        default: return nil
        }
    }

    /// The sounds the app had before remote settings existed; used until the server says otherwise.
    var builtIn: SoundSpec {
        switch self {
        case .urgent:
            return SoundSpec(wave: "square", peak: 0.7, attackS: 0.005, releaseS: 0.015,
                             notes: [[2000, 0, 0.135], [2000, 0.17, 0.135], [2000, 0.34, 0.135]])
        case .chime:
            return SoundSpec(wave: "triangle", peak: 0.65, attackS: 0.015, releaseS: 0.25,
                             notes: [[988, 0, 0.45], [1319, 0.17, 0.45]])
        case .call:
            // Softer and lower than the chime: two sine notes, under half its level.
            return SoundSpec(wave: "sine", peak: 0.3, attackS: 0.04, releaseS: 0.35,
                             notes: [[523, 0, 0.45], [659, 0.22, 0.45]])
        case .warning:
            return SoundSpec(wave: "sine", peak: 0.5, attackS: 0.03, releaseS: 0.52, notes: [[392, 0, 0.55], [294, 0.26, 0.55]])
        case .linkLost:
            return SoundSpec(wave: "sine", peak: 0.5, attackS: 0.03, releaseS: 0.47,
                             notes: [[523, 0, 0.5], [392, 0.22, 0.5], [294, 0.44, 0.5]])
        case .marked:
            return SoundSpec(wave: "sine", peak: 0.5, attackS: 0.005, releaseS: 0.05,
                             notes: [[1175, 0, 0.08], [1568, 0.09, 0.12]])
        }
    }
}

/// Turns a sound description into samples. Pure, so tests can check every sound without audio.
enum SoundSynth {
    static func render(_ spec: SoundSpec, rate: Double = 44_100) -> [Float] {
        let frames = Int((spec.totalSeconds + 0.02) * rate)
        guard frames > 0 else { return [] }
        var out = [Float](repeating: 0, count: frames)
        for note in spec.notes {
            let (frequency, start, length) = (note[0], note[1], note[2])
            let begin = Int(start * rate)
            let count = min(Int(length * rate), frames - begin)
            guard count > 0 else { continue }
            for i in 0..<count {
                let t = Double(i) / rate
                var level = 1.0
                if spec.attackS > 0, t < spec.attackS { level = t / spec.attackS }
                let remaining = length - t
                if spec.releaseS > 0, remaining < spec.releaseS { level = min(level, max(0, remaining / spec.releaseS)) }
                out[begin + i] += Float(level * wave(spec, frequency, t))
            }
        }
        let top = out.map(abs).max() ?? 0
        guard top > 0 else { return out }
        let gain = Float(spec.peak) / top
        return out.map { $0 * gain }
    }

    private static func wave(_ spec: SoundSpec, _ frequency: Double, _ t: Double) -> Double {
        let phase = (t * frequency).truncatingRemainder(dividingBy: 1)
        switch spec.wave {
        case "square": return phase < 0.5 ? 1 : -1
        case "triangle": return 4 * abs(phase - 0.5) - 1
        case "bell":
            let fade = spec.fadeS ?? 0.45
            let partials: [(Double, Double, Double)] = [(1, 1, 1), (2, 0.28, 0.49), (3, 0.1, 0.31), (4.07, 0.05, 0.2)]
            return partials.reduce(0) { sum, p in
                sum + p.1 * exp(-t / (fade * p.2)) * sin(2 * .pi * frequency * p.0 * t)
            }
        default: return sin(2 * .pi * frequency * t)
        }
    }
}
