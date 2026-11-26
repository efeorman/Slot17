import SwiftUI
import UIKit

// MARK: Parts shared by the portrait screen and the dashboard

struct StatusHeader: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack(spacing: 12) {
            StatusDot(level: model.headline.level, size: 16)
            VStack(alignment: .leading, spacing: 2) {
                Text(model.headline.title).font(.title3.bold()).foregroundColor(Theme.text)
                    .accessibilityIdentifier("headline")
                Text(model.headline.detail).font(.footnote).foregroundColor(Theme.dim)
                    .lineLimit(2).minimumScaleFactor(0.8)
            }
            Spacer(minLength: 4)
            MuteButton()
        }
    }
}

struct MuteButton: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Button { model.toggleMute() } label: {
            ZStack(alignment: .bottomTrailing) {
                Image(systemName: model.mutedSeconds > 0 || !model.settings.sound ? "speaker.slash.fill" : "speaker.wave.2.fill")
                    .font(.title3)
                    .foregroundColor(model.mutedSeconds > 0 ? Theme.yellow : Theme.text)
                    .frame(width: 48, height: 48)
                    .controlSurface(Theme.raised, cornerRadius: 12)
                if model.mutedSeconds > 0 {
                    Text("\(model.mutedSeconds)").font(.system(size: 11, weight: .bold).monospacedDigit())
                        .foregroundColor(Theme.yellow).padding(3)
                }
            }
        }
        .accessibilityLabel(model.mutedSeconds > 0 ? "Unmute" : "Mute for 30 seconds")
        .accessibilityIdentifier("mute")
    }
}

/// Phone to receiver, each link with its own state, so a failure names where it is.
struct ChainView: View {
    @EnvironmentObject private var model: AppModel
    var vertical = false

    var body: some View {
        let layout = vertical ? AnyLayout(VStackLayout(alignment: .leading, spacing: 10))
                              : AnyLayout(HStackLayout(alignment: .top, spacing: 6))
        layout {
            ForEach(model.chain) { item in
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 5) {
                        StatusDot(level: item.level, size: 8)
                        Text(item.name).font(.caption.weight(.semibold)).foregroundColor(Theme.text)
                    }
                    Text(item.detail).font(.caption2).foregroundColor(Theme.dim)
                        .lineLimit(2).minimumScaleFactor(0.8)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier("chain-\(item.id)")
            }
        }
    }
}

struct SegmentMeter: View {
    let row: MeterRow?
    let grey: Bool
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var pulse: CGFloat = 0
    @State private var lastPulse: [Int: Date] = [:]

    /// What VoiceOver says: words instead of symbols such as "×/min" and "≈", in reading order.
    private func spoken(strong: Bool, style: DisplayStyle) -> String {
        guard let row else { return "No channel" }
        var parts = ["Channel L\(row.ch)"]
        if strong { parts.append(style.strongBadge) }
        parts.append(row.strength.map { String(format: "%.0f decibels above the noise", $0) } ?? "strength not available")
        parts.append((row.approxTime ? "about " : "") + Format.ago(row.age).replacingOccurrences(of: " s ago", with: " seconds ago"))
        parts.append("\(row.hits60) a minute")
        if row.call && !grey {
            parts.append("in a call")
        } else if row.frequent && !grey {
            parts.append(style.frequentText)
        }
        if row.rising && !grey { parts.append("rising") }
        return parts.joined(separator: ", ")
    }

    var body: some View {
        let settings = model.settings
        let style = model.style
        let lit = Meters.level(row?.strength, urgent: settings.urgentLevel)
        let kind = Meters.colour(row?.strength, settings)
        let colour = grey ? Theme.grey : Theme.colour(kind, style)
        let strong = !grey && Meters.isStrong(row?.strength, settings)
        // Dimmer at night: the screen's own brightness is the best sign of a dark car.
        let night = UIScreen.main.brightness < 0.3
        let outline = (Theme.hex(style.strongOutline) ?? .white).opacity(night ? 0.55 : 0.9)
        VStack(spacing: 8) {
            // A strip every meter keeps, so the badge never covers a segment and the bars stay level.
            ZStack {
                if strong {
                    Text(style.strongBadge)
                        .font(.caption2.weight(.heavy)).tracking(0.5)
                        .foregroundColor(.black)
                        .lineLimit(1).minimumScaleFactor(0.6)
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(outline).clipShape(Capsule())
                }
            }
            .frame(height: 18)
            GeometryReader { geometry in
                let gap: CGFloat = 4
                let height = max(3, (geometry.size.height - gap * CGFloat(Meters.segments - 1)) / CGFloat(Meters.segments))
                VStack(spacing: gap) {
                    ForEach((0..<Meters.segments).reversed(), id: \.self) { index in
                        RoundedRectangle(cornerRadius: 4)
                            .fill(index < lit ? colour : Theme.segmentOff)
                            .frame(height: height)
                            .shadow(color: index < lit && kind == .red && !grey ? colour.opacity(0.4) : .clear, radius: 5)
                    }
                }
                // Segments rise and fall instead of jumping; colour changes cross-fade.
                .animation(reduceMotion ? nil : .easeOut(duration: 0.28), value: lit)
                .animation(reduceMotion ? nil : .easeInOut(duration: 0.25), value: kind)
            }
            .frame(maxWidth: 74)
            if let row {
                Text("L\(row.ch)").font(.headline.monospacedDigit()).foregroundColor(Theme.text)
                Text(row.strength.map { String(format: "%.1f dB", $0) } ?? "strength n/a")
                    .font(.subheadline.weight(.semibold).monospacedDigit()).foregroundColor(Theme.text)
                    .lineLimit(1).minimumScaleFactor(0.7)
                    .contentTransition(.numericText())
                    .animation(reduceMotion ? nil : .easeOut(duration: 0.25), value: row.strength)
                Text(String(format: "%.4f MHz", row.freqMHz)).font(.caption2.monospacedDigit()).foregroundColor(Theme.dim)
                    .lineLimit(1).minimumScaleFactor(0.7)
                Text("\(row.approxTime ? "≈ " : "")\(Format.ago(row.age)) · \(row.hits60)×/min")
                    .font(.caption2.monospacedDigit()).foregroundColor(Theme.dim).lineLimit(1).minimumScaleFactor(0.7)
                if row.call && !grey {
                    Label("In a call", systemImage: "phone.and.waveform")
                        .font(.caption2.weight(.semibold)).foregroundColor(Theme.text)
                        .lineLimit(1).minimumScaleFactor(0.6)
                } else if row.frequent && !grey {
                    Label(style.frequentText, systemImage: "waveform")
                        .font(.caption2.weight(.semibold)).foregroundColor(Theme.text)
                        .lineLimit(1).minimumScaleFactor(0.6)
                }
                if row.rising && !grey {
                    Label("Rising", systemImage: "arrow.up.right")
                        .font(.caption2.weight(.semibold)).foregroundColor(Theme.text)
                        .lineLimit(1).minimumScaleFactor(0.6)
                }
            } else {
                Text("—").font(.headline).foregroundColor(Theme.dim)
            }
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 6)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.panel)
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(strong ? outline : Theme.line, lineWidth: strong ? 3 : 1))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(outline.opacity(Double(pulse)), lineWidth: 3 + 6 * pulse))
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .opacity(row.map { Meters.opacity($0.age) } ?? 0.45)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(spoken(strong: strong, style: style))
        .accessibilityIdentifier(strong ? "meter-strong" : "meter")
        .onChange(of: freshStrongKey(strong)) { key in
            guard let key, style.pulseOnNewStrong, !reduceMotion else { return }
            let now = Date()
            if let last = lastPulse[key], now.timeIntervalSince(last) < Meters.pulseEvery { return }
            lastPulse[key] = now
            pulse = 1
            withAnimation(.easeOut(duration: 1)) { pulse = 0 }
        }
    }

    /// The channel, while a strong burst on it is fresh; nil otherwise. A change to non-nil starts one pulse.
    private func freshStrongKey(_ strong: Bool) -> Int? {
        guard strong, let row, row.age < Meters.pulseFreshAge else { return nil }
        return row.ch
    }
}

struct MeterRowView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack(spacing: 8) {
            ForEach(0..<3, id: \.self) { index in
                SegmentMeter(row: index < model.meters.count ? model.meters[index] : nil, grey: !model.showBars)
            }
        }
    }
}

struct Banners: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        if model.demo {
            Text("DEMO. NOT REAL DETECTIONS").font(.footnote.bold()).foregroundColor(.black)
                .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity).padding(6).background(Theme.yellow).clipShape(RoundedRectangle(cornerRadius: 8))
        }
        if !model.banner.isEmpty {
            HStack(spacing: 8) {
                Image(systemName: "info.circle.fill")
                Text(model.banner).fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .font(.footnote.weight(.semibold)).foregroundColor(.black)
            .padding(8).background(Theme.blue).clipShape(RoundedRectangle(cornerRadius: 8))
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("server-message")
        }
        if model.settings.quiet {
            HStack(spacing: 6) {
                Image(systemName: "moon.fill")
                Text(String(format: "Quiet mode: chimes from %.0f dB. Urgent beeps unchanged.", model.settings.effectiveRed))
            }
            .font(.caption.weight(.semibold)).foregroundColor(Theme.dim)
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityIdentifier("quiet-indicator")
        }
        if model.receiverQuiet {
            Text("Receiver hears nothing. Check the antenna and its cable.").font(.footnote.bold()).foregroundColor(.white)
                .frame(maxWidth: .infinity).padding(8).background(Theme.red).clipShape(RoundedRectangle(cornerRadius: 8))
                .accessibilityIdentifier("receiver-banner")
        }
    }
}

struct MarkButton: View {
    let label: Sighting.Label
    @EnvironmentObject private var model: AppModel

    private var symbol: String {
        switch label {
        case .police: return "light.beacon.max.fill"
        case .ambulance: return "cross.case.fill"
        case .fire: return "flame.fill"
        case .unknown: return "questionmark.circle.fill"
        case .nothing: return "eye.slash.fill"
        }
    }

    private var tint: Color {
        switch label {
        case .police: return Theme.blue
        case .ambulance: return Theme.yellow
        case .fire: return Theme.red
        case .unknown, .nothing: return Theme.dim
        }
    }

    var compact = false

    var body: some View {
        Button {
            // A firm tap you can feel without looking: the mark is taken.
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            model.mark(label)
        } label: {
            Group {
                if label == .nothing && !compact {
                    // A wide, quieter button: for after a sound with nothing in sight.
                    HStack(spacing: 8) {
                        Image(systemName: symbol).foregroundColor(tint)
                        Text("Saw nothing after the beep").font(.footnote.weight(.semibold)).foregroundColor(Theme.text)
                    }
                    .frame(maxWidth: .infinity, minHeight: 44)
                } else {
                    VStack(spacing: compact ? 3 : 6) {
                        Image(systemName: symbol).font(compact ? .body : .title2).foregroundColor(tint)
                        Text(label.title).font((compact ? Font.caption2 : .footnote).weight(.semibold)).foregroundColor(Theme.text)
                            .lineLimit(1).minimumScaleFactor(0.6)
                    }
                    .frame(maxWidth: .infinity, minHeight: compact ? 46 : 66)
                }
            }
            .controlSurface(Theme.raised, cornerRadius: 14)
        }
        .buttonStyle(PressableStyle())
        .accessibilityLabel("I saw: \(label.title)")
        .accessibilityIdentifier("mark-\(label.rawValue)")
    }
}

struct LastSightingLine: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        if let last = model.sightings.first, Date().timeIntervalSince(last.seenAt) < 120 {
            HStack(spacing: 6) {
                Image(systemName: last.phase == .failed ? "exclamationmark.triangle.fill" : (last.phase == .saved ? "checkmark.circle.fill" : "clock.fill"))
                    .foregroundColor(last.phase == .failed ? Theme.red : (last.phase == .saved ? Theme.green : Theme.dim))
                Text("\(last.label.title): \(last.statusText)").font(.footnote).foregroundColor(Theme.dim)
                    .lineLimit(2).minimumScaleFactor(0.8)
                    .accessibilityIdentifier("last-sighting")
            }
        }
    }
}

struct LastAlertCard: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        let last = model.alerts.first { $0.sound != nil }
        HStack(spacing: 10) {
            Image(systemName: "bell.fill").foregroundColor(last?.sound == .urgent ? Theme.red : Theme.dim)
            if let last {
                VStack(alignment: .leading, spacing: 1) {
                    Text(AlertText.title(last)).font(.subheadline.weight(.semibold)).foregroundColor(Theme.text)
                    Text(last.at, style: .relative).font(.caption).foregroundColor(Theme.dim) + Text(" ago").font(.caption).foregroundColor(Theme.dim)
                }
            } else {
                Text("No alert yet").font(.subheadline).foregroundColor(Theme.dim)
            }
            Spacer()
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .background(Theme.panel)
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.line, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("last-alert")
    }
}

enum AlertText {
    static func title(_ a: AlertRecord) -> String {
        let db = a.strength.map { String(format: "%.1f dB", $0) } ?? "no strength"
        switch a.sound {
        case .urgent?: return "Urgent beep · L\(a.channel) · \(db)"
        case .chime?: return "Chime · L\(a.channel) · \(db)"
        case .call?: return "Call tone · L\(a.channel) · \(db)"
        default: return "Silent · L\(a.channel) · \(db)"
        }
    }
}

// MARK: Portrait

struct LiveView: View {
    @EnvironmentObject private var model: AppModel
    let openDashboard: () -> Void

    var body: some View {
        GeometryReader { geometry in
            ScrollView {
                VStack(spacing: 12) {
                    StatusHeader()
                    ChainView().padding(12).background(Theme.panel)
                        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.line, lineWidth: 1))
                        .clipShape(RoundedRectangle(cornerRadius: 14))
                    Banners()
                    MeterRowView().frame(height: max(230, geometry.size.height - 445))
                    if model.moreChannels > 0 {
                        MoreChannels()
                    } else if model.showBars && model.meters.isEmpty {
                        Text("No TETRA detected").font(.footnote).foregroundColor(Theme.dim)
                    }
                    HStack(spacing: 8) {
                        LastAlertCard()
                        Button(action: openDashboard) {
                            Image(systemName: "rectangle.landscape.rotate").font(.body.weight(.semibold))
                                .foregroundColor(Theme.text)
                                .frame(width: 48, height: 42)
                                .controlSurface(Theme.raised, cornerRadius: 12)
                        }
                        .accessibilityLabel("Open the dashboard")
                        .accessibilityIdentifier("open-dashboard")
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text("I SAW").font(.caption.weight(.semibold)).foregroundColor(Theme.dim)
                        HStack(spacing: 8) { ForEach(Sighting.Label.vehicles) { MarkButton(label: $0) } }
                        MarkButton(label: .nothing)
                        LastSightingLine()
                    }
                    Text(model.soundLine).font(.caption).foregroundColor(model.soundOK ? Theme.dim : Theme.red)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityIdentifier("sound-line")
                    Text("Signal strength above channel noise. Not a distance, not a specific vehicle.")
                        .font(.caption2).foregroundColor(Theme.dim).frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.horizontal, 16).padding(.vertical, 8)
            }
        }
    }
}

// MARK: Landscape dashboard

struct DashboardView: View {
    @EnvironmentObject private var model: AppModel
    let close: (() -> Void)?

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) {
                    StatusDot(level: model.headline.level, size: 14)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(model.headline.title).font(.headline).foregroundColor(Theme.text)
                            .lineLimit(1).minimumScaleFactor(0.7)
                            .accessibilityIdentifier("headline")
                        Text(model.headline.detail).font(.caption2).foregroundColor(Theme.dim)
                            .lineLimit(2).minimumScaleFactor(0.8)
                    }
                }
                Banners()
                ChainView(vertical: true)
                Spacer(minLength: 0)
                HStack(spacing: 8) {
                    MuteButton()
                    if close == nil {
                        Button { model.tab = .settings } label: {
                            Image(systemName: "gearshape.fill").font(.title3).foregroundColor(Theme.text)
                                .frame(width: 48, height: 48).controlSurface(Theme.raised, cornerRadius: 12)
                        }
                        .accessibilityLabel("Open Settings")
                        .accessibilityIdentifier("dashboard-settings")
                    }
                    if let close {
                        Button(action: close) {
                            Image(systemName: "rectangle.portrait.rotate").font(.title3).foregroundColor(Theme.text)
                                .frame(width: 48, height: 48).controlSurface(Theme.raised, cornerRadius: 12)
                        }
                        .accessibilityLabel("Close the dashboard")
                        .accessibilityIdentifier("close-dashboard")
                    }
                }
            }
            .frame(width: 170)

            VStack(spacing: 6) {
                MeterRowView()
                Text("Signal strength above channel noise. Not a distance, not a specific vehicle.")
                    .font(.caption2).foregroundColor(Theme.dim).lineLimit(1).minimumScaleFactor(0.7)
            }

            VStack(spacing: 8) {
                Text("I SAW").font(.caption2.weight(.semibold)).foregroundColor(Theme.dim)
                ForEach(Sighting.Label.vehicles) { MarkButton(label: $0, compact: true) }
                MarkButton(label: .nothing, compact: true)
                LastSightingLine()
            }
            .frame(width: 92)
        }
        .padding(10)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.background)
    }
}

/// The channels beyond the three meters, as small chips: channel, strength, age, call or rising.
struct MoreChannels: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        let rows = model.extraMeters
        let hidden = model.moreChannels - rows.count
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(rows) { row in chip(row) }
                if hidden > 0 {
                    Text("+\(hidden)").font(.caption.weight(.semibold)).foregroundColor(Theme.dim)
                }
            }
        }
        .accessibilityIdentifier("more-channels")
        .accessibilityLabel("\(model.moreChannels) more channel\(model.moreChannels > 1 ? "s" : "")")
    }

    private func chip(_ row: MeterRow) -> some View {
        let colour = Theme.colour(Meters.colour(row.strength, model.settings), model.style)
        return HStack(spacing: 4) {
            Circle().fill(colour).frame(width: 7, height: 7)
            Text("L\(row.ch)").font(.caption.weight(.bold).monospacedDigit()).foregroundColor(Theme.text)
            if let db = row.strength {
                Text(String(format: "%.0f dB", db)).font(.caption.monospacedDigit()).foregroundColor(Theme.text)
            }
            Text(Format.ago(row.age)).font(.caption2.monospacedDigit()).foregroundColor(Theme.dim)
            if row.call { Image(systemName: "phone.and.waveform").font(.caption2).foregroundColor(Theme.text) }
            if row.rising { Image(systemName: "arrow.up.right").font(.caption2).foregroundColor(Theme.text) }
        }
        .padding(.horizontal, 8).padding(.vertical, 5)
        .background(Theme.panel)
        .overlay(Capsule().stroke(Theme.line, lineWidth: 1))
        .clipShape(Capsule())
        .opacity(Meters.opacity(row.age))
    }
}
