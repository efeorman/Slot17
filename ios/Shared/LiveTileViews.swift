import SwiftUI

/// The Live Activity's look, shared by the widget extension and the app's own preview screen.
/// The system decides the timing of Live Activity animations, and Always-On shows none, so these views
/// rely on content transitions and self-updating timer text: the "seconds ago" counts up on the device
/// without any update from the app.
enum LiveTilePalette {
    static let background = Color(red: 0.043, green: 0.055, blue: 0.075)
    static let panel = Color(red: 0.075, green: 0.094, blue: 0.129)
    static let off = Color(red: 0.16, green: 0.19, blue: 0.24)
    static let text = Color(red: 0.93, green: 0.95, blue: 0.97)
    static let dim = Color(red: 0.62, green: 0.67, blue: 0.73)
    static let caution = Color(red: 0.961, green: 0.773, blue: 0.259)

    static func colour(_ hex: String, _ fallback: Color) -> Color {
        guard hex.count == 7, hex.first == "#", let v = UInt32(hex.dropFirst(), radix: 16) else { return fallback }
        return Color(red: Double((v >> 16) & 0xFF) / 255, green: Double((v >> 8) & 0xFF) / 255, blue: Double(v & 0xFF) / 255)
    }
}

extension LiveTileState {
    private func hex(_ index: Int, _ fallback: Color) -> Color {
        index < colours.count ? LiveTilePalette.colour(colours[index], fallback) : fallback
    }
    var toneColour: Color {
        switch tone {
        case .none: return LiveTilePalette.dim
        case .weak: return hex(0, .gray)
        case .silent: return hex(1, .yellow)
        case .chime, .strong: return hex(2, .red)
        }
    }
    var outlineColour: Color { hex(3, .white) }

    /// One short phrase per phase, for places with no room for more.
    var phaseTitle: String {
        switch phase {
        case .listening: return "Listening"
        case .signal: return channel.map { "L\($0)" } ?? "Signal"
        case .linkLost: return "No link"
        case .stopped: return "Stopped"
        case .receiverQuiet: return "Receiver quiet"
        case .ended: return "Ended"
        }
    }

    var phaseDetail: String {
        switch phase {
        case .listening: return "Connected · nothing recent"
        case .signal: return ""
        case .linkLost: return "Silence means nothing now"
        case .stopped: return "The detector is not running"
        case .receiverQuiet: return "Check antenna and cable"
        case .ended: return "Drive over"
        }
    }

    var phaseSymbol: String {
        switch phase {
        case .listening: return "dot.radiowaves.left.and.right"
        case .signal: return "antenna.radiowaves.left.and.right"
        case .linkLost: return "wifi.exclamationmark"
        case .stopped: return "stop.circle"
        case .receiverQuiet: return "antenna.radiowaves.left.and.right.slash"
        case .ended: return "flag.checkered"
        }
    }

    /// The icon for what the app did with this signal, so the meaning does not rest on colour alone.
    /// Never a vehicle or a police light: the detector cannot know who transmitted.
    var actionSymbol: String {
        guard phase == .signal else { return phaseSymbol }
        switch tone {
        case .none, .weak: return "antenna.radiowaves.left.and.right"
        case .silent: return "speaker.slash.fill"
        case .chime: return "bell.fill"
        case .strong: return "bell.and.waves.left.and.right.fill"
        }
    }

    var actionColour: Color {
        if phase == .signal { return tone == .none ? LiveTilePalette.dim : toneColour }
        return phaseIsProblem ? LiveTilePalette.caution : LiveTilePalette.dim
    }

    /// Phases that need the driver's attention get the caution colour.
    var phaseIsProblem: Bool { phase == .linkLost || phase == .stopped || phase == .receiverQuiet }
}

/// A vertical 10-segment meter, as in the app.
struct LiveTileMeter: View {
    let state: LiveTileState
    var spacing: CGFloat = 2
    var dimmed = false

    var body: some View {
        GeometryReader { geometry in
            let count = 10
            let height = max(1.5, (geometry.size.height - spacing * CGFloat(count - 1)) / CGFloat(count))
            VStack(spacing: spacing) {
                ForEach((0..<count).reversed(), id: \.self) { index in
                    RoundedRectangle(cornerRadius: min(3, height / 2))
                        .fill(index < state.level ? state.toneColour.opacity(dimmed ? 0.55 : 1) : LiveTilePalette.off)
                        .frame(height: height)
                }
            }
        }
        .accessibilityHidden(true)
    }
}

/// The STRONG badge, in the server's words and outline colour.
struct LiveTileBadge: View {
    let state: LiveTileState
    var small = false

    var body: some View {
        Text(state.badge)
            .font(.system(size: small ? 9 : 11, weight: .heavy))
            .tracking(0.4)
            .lineLimit(1)
            .minimumScaleFactor(0.6)
            .foregroundColor(.black)
            .padding(.horizontal, small ? 4 : 6)
            .padding(.vertical, small ? 1 : 2)
            .background(Capsule().fill(state.outlineColour))
    }
}

/// The large round icon at the end of a tile.
struct LiveTileActionIcon: View {
    let state: LiveTileState
    var isStale = false
    var size: CGFloat = 36

    var body: some View {
        let colour = isStale ? LiveTilePalette.caution : state.actionColour
        ZStack {
            Circle().fill(colour.opacity(0.18))
            Image(systemName: isStale ? "clock.badge.exclamationmark" : state.actionSymbol)
                .font(.system(size: size * 0.46, weight: .semibold))
                .foregroundColor(colour)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// "0:04" since a date, counting up on the device by itself.
struct LiveTileClock: View {
    let since: Date
    var alignment: TextAlignment = .leading
    var body: some View {
        Text(timerInterval: since...Date.distantFuture, countsDown: false)
            .monospacedDigit()
            .multilineTextAlignment(alignment)
    }
}

/// The update's number, small: read it from a photo and compare with the log. See `LiveTileState.rev`.
struct LiveTileRevision: View {
    let rev: Int?
    var body: some View {
        if let rev {
            Text("R\(rev)")
                .font(.system(size: 9, weight: .medium, design: .rounded))
                .monospacedDigit()
                .lineLimit(1)
                .foregroundColor(LiveTilePalette.dim)
                .accessibilityHidden(true)
        }
    }
}

/// "Police" with a tick: the sighting was taken. Shown briefly, instead of the last beep.
struct LiveTileMarkLine: View {
    let mark: LiveTileState.Mark
    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: "checkmark.circle.fill")
            Text("You marked: \(mark.title)").fontWeight(.semibold).lineLimit(1).minimumScaleFactor(0.7)
            if let context = mark.context {
                Text("· \(context)").foregroundColor(LiveTilePalette.dim).lineLimit(1).minimumScaleFactor(0.7)
            }
        }
        .foregroundColor(LiveTilePalette.text)
    }
}

/// The car's data is a few seconds late: what the tile shows may be behind.
struct LiveTileLateLine: View {
    var body: some View {
        Label("Car data late", systemImage: "hourglass")
            .foregroundColor(LiveTilePalette.caution).lineLimit(1).minimumScaleFactor(0.7)
    }
}

/// "Updated to a46b01c": the car computer's software changed, or could not.
struct LiveTileSoftwareLine: View {
    let software: LiveTileState.Software
    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: software.problem ? "exclamationmark.triangle.fill" : "arrow.down.circle.fill")
            Text(software.text).fontWeight(.semibold).lineLimit(1).minimumScaleFactor(0.7)
        }
        .foregroundColor(software.problem ? LiveTilePalette.caution : LiveTilePalette.text)
    }
}

/// The Lock Screen and banner presentation (iPhone), at most 160 pt tall by Apple's rules.
struct LiveTileLockScreen: View {
    let state: LiveTileState
    var isStale = false
    var reduced = false

    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            LiveTileMeter(state: state, spacing: 2.5, dimmed: reduced || isStale)
                .frame(width: 22, height: 86)
            VStack(alignment: .leading, spacing: 5) {
                header
                detail
                footer
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            VStack(spacing: 3) {
                LiveTileActionIcon(state: state, isStale: isStale, size: 40)
                LiveTileRevision(rev: state.rev)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .overlay(
            RoundedRectangle(cornerRadius: 22)
                .strokeBorder(state.outlineColour.opacity(state.tone == .strong && !isStale ? 0.9 : 0), lineWidth: 2.5)
        )
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            if state.phase == .signal, let channel = state.channel {
                Text("L\(channel)")
                    .font(.system(size: 26, weight: .bold, design: .rounded))
                    .foregroundColor(isStale ? LiveTilePalette.dim : LiveTilePalette.text)
                    .contentTransition(.numericText())
                if let strength = state.strength {
                    Text("\(strength) dB")
                        .font(.system(size: 20, weight: .semibold, design: .rounded))
                        .foregroundColor(isStale ? LiveTilePalette.dim : state.toneColour)
                        .contentTransition(.numericText())
                }
                if state.tone == .strong && !isStale { LiveTileBadge(state: state) }
                if let others = state.others, !isStale {
                    Text("+\(others)").font(.system(size: 15, weight: .bold, design: .rounded))
                        .foregroundColor(LiveTilePalette.dim)
                }
            } else {
                // The round icon at the end of the tile already shows this phase.
                Text(state.phaseTitle)
                    .font(.system(size: 22, weight: .bold, design: .rounded))
                    .foregroundColor(state.phaseIsProblem ? LiveTilePalette.caution : LiveTilePalette.text)
            }
            Spacer(minLength: 4)
            if state.demo {
                Text("DEMO").font(.caption2.weight(.heavy)).foregroundColor(LiveTilePalette.caution)
            }
        }
    }

    @ViewBuilder private var detail: some View {
        if isStale {
            Label("Not updating · open Slot17", systemImage: "clock.badge.exclamationmark")
                .font(.footnote.weight(.semibold))
                .foregroundColor(LiveTilePalette.caution)
        } else if state.phase == .signal, let heard = state.heardAt {
            HStack(spacing: 5) {
                Image(systemName: "clock")
                LiveTileClock(since: heard).frame(width: 44, alignment: .leading)
                if state.call == true {
                    Image(systemName: "phone.and.waveform")
                    Text("In a call")
                } else if state.frequent {
                    Image(systemName: "waveform")
                    Text("Frequent bursts")
                }
                if state.rising == true {
                    Image(systemName: "arrow.up.right")
                    Text("Rising")
                }
            }
            .font(.footnote)
            .foregroundColor(LiveTilePalette.dim)
            .lineLimit(1)
            .minimumScaleFactor(0.8)
        } else if state.phase == .listening, let last = state.lastHeard {
            HStack(spacing: 5) {
                Text("Last heard L\(last.channel)")
                LiveTileClock(since: last.at).frame(width: 52, alignment: .leading)
            }
            .font(.footnote).foregroundColor(LiveTilePalette.dim).lineLimit(1)
        } else {
            Text(state.phaseDetail).font(.footnote).foregroundColor(LiveTilePalette.dim).lineLimit(1)
        }
    }

    @ViewBuilder private var footer: some View {
        if state.slow == true && !isStale {
            LiveTileLateLine().font(.caption)
        } else if let mark = state.mark {
            LiveTileMarkLine(mark: mark).font(.caption)
        } else if let software = state.software {
            LiveTileSoftwareLine(software: software).font(.caption)
        } else if let alert = state.lastAlert {
            HStack(spacing: 5) {
                Image(systemName: "bell.fill")
                    .foregroundColor(alert.urgent ? state.outlineColour : LiveTilePalette.dim)
                Text(alert.urgent ? "Urgent" : "Chime").fontWeight(.semibold)
                Text("L\(alert.channel)")
                if let strength = alert.strength { Text("\(strength) dB") }
                Spacer(minLength: 4)
                LiveTileClock(since: alert.at, alignment: .trailing).frame(width: 48, alignment: .trailing)
            }
            .font(.caption)
            .foregroundColor(LiveTilePalette.text.opacity(0.85))
            .lineLimit(1)
        } else {
            Text("No beep yet").font(.caption).foregroundColor(LiveTilePalette.dim)
        }
    }
}

/// The small presentation: the CarPlay Dashboard (and Apple Watch). Few, large things only.
struct LiveTileSmall: View {
    let state: LiveTileState
    var isStale = false
    var reduced = false

    var body: some View {
        HStack(spacing: 8) {
            LiveTileMeter(state: state, spacing: 1.5, dimmed: reduced || isStale)
                .frame(width: 12)
            VStack(alignment: .leading, spacing: 2) {
                if state.demo {
                    Text("DEMO").font(.system(size: 10, weight: .heavy)).foregroundColor(LiveTilePalette.caution)
                }
                if state.phase == .signal, let channel = state.channel {
                    HStack(spacing: 4) {
                        Text("L\(channel)")
                            .font(.system(size: 20, weight: .bold, design: .rounded))
                            .contentTransition(.numericText())
                        if state.tone == .strong && !isStale { LiveTileBadge(state: state, small: true) }
                        if let others = state.others, !isStale {
                            Text("+\(others)").font(.system(size: 12, weight: .bold, design: .rounded))
                                .foregroundColor(LiveTilePalette.dim)
                        }
                    }
                    .foregroundColor(isStale ? LiveTilePalette.dim : LiveTilePalette.text)
                    HStack(spacing: 4) {
                        if let strength = state.strength {
                            Text("\(strength) dB").foregroundColor(isStale ? LiveTilePalette.dim : state.toneColour)
                                .contentTransition(.numericText())
                        }
                        if state.call == true && !isStale {
                            Image(systemName: "phone.and.waveform").foregroundColor(LiveTilePalette.text)
                        } else if state.frequent && !isStale {
                            Image(systemName: "waveform").foregroundColor(LiveTilePalette.text)
                        }
                        if state.rising == true && !isStale {
                            Image(systemName: "arrow.up.right").foregroundColor(LiveTilePalette.text)
                        }
                        if let heard = state.heardAt, !isStale {
                            Image(systemName: "clock").foregroundColor(LiveTilePalette.dim)
                            LiveTileClock(since: heard).foregroundColor(LiveTilePalette.dim)
                                .frame(width: 40, alignment: .leading)
                        }
                    }
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                } else {
                    // The round icon at the end of the tile already shows this phase.
                    Text(state.phaseTitle)
                        .font(.system(size: 15, weight: .bold, design: .rounded))
                        .foregroundColor(state.phaseIsProblem ? LiveTilePalette.caution : LiveTilePalette.text)
                    if state.phase == .listening, let last = state.lastHeard, !isStale {
                        HStack(spacing: 3) {
                            Text("Last L\(last.channel)")
                            LiveTileClock(since: last.at).frame(width: 40, alignment: .leading)
                        }
                        .font(.system(size: 11, weight: .medium)).foregroundColor(LiveTilePalette.dim)
                    }
                }
                if isStale {
                    Text("Not updating").font(.system(size: 11, weight: .semibold)).foregroundColor(LiveTilePalette.caution)
                } else if state.slow == true {
                    LiveTileLateLine().font(.system(size: 11, weight: .semibold))
                } else if let mark = state.mark {
                    LiveTileMarkLine(mark: mark).font(.system(size: 11, weight: .medium))
                } else if let software = state.software {
                    LiveTileSoftwareLine(software: software).font(.system(size: 11, weight: .medium))
                } else if let alert = state.lastAlert {
                    HStack(spacing: 4) {
                        Image(systemName: "bell.fill")
                        Text(alert.urgent ? "Urgent" : "Chime")
                        LiveTileClock(since: alert.at).frame(width: 36, alignment: .leading)
                    }
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(alert.urgent ? state.outlineColour : LiveTilePalette.dim)
                }
            }
            .lineLimit(1)
            .minimumScaleFactor(0.7)
            Spacer(minLength: 2)
            VStack(spacing: 2) {
                LiveTileActionIcon(state: state, isStale: isStale)
                LiveTileRevision(rev: state.rev)
            }
        }
        .padding(8)
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .strokeBorder(state.outlineColour.opacity(state.tone == .strong && !isStale ? 0.9 : 0), lineWidth: 2)
        )
    }
}

/// The Dynamic Island's compact and minimal pieces.
struct LiveTileIslandMark: View {
    let state: LiveTileState
    var isStale = false
    var body: some View {
        if isStale {
            Image(systemName: "clock.badge.exclamationmark").foregroundColor(LiveTilePalette.caution)
        } else {
            Image(systemName: state.actionSymbol).foregroundColor(state.actionColour)
        }
    }
}

struct LiveTileIslandValue: View {
    let state: LiveTileState
    var isStale = false
    var body: some View {
        Group {
            if state.demo {
                Text("DEMO").font(.system(size: 10, weight: .heavy)).foregroundColor(LiveTilePalette.caution)
            } else if isStale {
                // Old numbers must never look current.
                Text("–").foregroundColor(LiveTilePalette.caution)
            } else if state.phase == .signal, let strength = state.strength {
                Text("\(strength)")
                    .foregroundColor(state.tone == .strong ? state.outlineColour : state.toneColour)
                    .contentTransition(.numericText())
            } else {
                Text(state.phaseIsProblem ? "!" : "–")
                    .foregroundColor(state.phaseIsProblem ? LiveTilePalette.caution : LiveTilePalette.dim)
            }
        }
        .font(.system(.body, design: .rounded).weight(.bold))
        .monospacedDigit()
    }
}
