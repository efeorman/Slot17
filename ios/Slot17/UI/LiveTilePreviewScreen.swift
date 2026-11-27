import SwiftUI
import UIKit

struct LiveTilePreviewScreen: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss

    private func sample(_ phase: LiveTileState.Phase, ch: Int? = nil, db: Int? = nil, level: Int = 0,
                        tone: LiveTileState.Tone = .none, frequent: Bool = false, heard: Double? = nil,
                        alert: LiveTileState.Alert? = nil) -> LiveTileState {
        var s = LiveTileState.placeholder
        let style = model.style
        s.colours = [style.weak, style.silent, style.chime, style.strongOutline]
        s.badge = style.strongBadge
        s.phase = phase; s.channel = ch; s.strength = db; s.level = level; s.tone = tone; s.frequent = frequent
        s.heardAt = heard.map { Date().addingTimeInterval(-$0) }
        s.lastAlert = alert
        return s
    }

    /// Only a test may press the picture of the widget; for the operator it must not record anything.
    private var widgetButtonsWork: Bool {
        #if DEBUG
        return ProcessInfo.processInfo.arguments.contains("--real-widget-buttons")
        #else
        return false
        #endif
    }

    private var markedSample: LiveTileState {
        var s = sample(.signal, ch: 119, db: 47, level: 9, tone: .chime, heard: 4, alert: chimeAlert)
        s.mark = .init(title: "Police", at: Date())
        s.rev = 42
        return s
    }

    private var urgentAlert: LiveTileState.Alert { .init(urgent: true, channel: 154, strength: 55, at: Date().addingTimeInterval(-3)) }
    private var chimeAlert: LiveTileState.Alert { .init(urgent: false, channel: 102, strength: 30, at: Date().addingTimeInterval(-42)) }
    /// An aircraft's channel (1.8.0): the helicopter beside the channel and in place of the bell.
    private var airAlert: LiveTileState.Alert { .init(urgent: false, channel: 194, strength: 38, at: Date().addingTimeInterval(-6)) }
    private var airSample: LiveTileState { sample(.signal, ch: 194, db: 38, level: 7, tone: .chime, heard: 2, alert: airAlert) }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if #available(iOS 17.0, *) {
                        section("CarPlay widget") {
                            MarkWidgetContent()
                                .padding(8)
                                .frame(width: 170, height: 170)
                                .background(RoundedRectangle(cornerRadius: 22).fill(LiveTilePalette.background))
                                .allowsHitTesting(widgetButtonsWork)
                            Text("Add it on iPhone: Settings › General › CarPlay › your car. It sits on the page left of the map. A tap records the sighting and plays a short blip. These buttons here are only a picture.")
                                .font(.caption2).foregroundColor(Theme.dim)
                        }
                    }
                    section("CarPlay Dashboard") {
                        carTile(markedSample)
                        carTile(sample(.signal, ch: 154, db: 55, level: 10, tone: .strong, frequent: true, heard: 1, alert: urgentAlert))
                        carTile(airSample)
                        carTile(sample(.signal, ch: 119, db: 47, level: 9, tone: .chime, heard: 4, alert: chimeAlert))
                        carTile(sample(.signal, ch: 60, db: 15, level: 3, tone: .silent, heard: 7, alert: chimeAlert))
                        carTile(sample(.listening, alert: chimeAlert))
                        carTile(sample(.linkLost))
                    }
                    section("Lock Screen") {
                        lockTile(sample(.signal, ch: 154, db: 55, level: 10, tone: .strong, frequent: true, heard: 1, alert: urgentAlert))
                        lockTile(airSample)
                        lockTile(sample(.signal, ch: 60, db: 15, level: 3, tone: .silent, heard: 7, alert: chimeAlert))
                        lockTile(sample(.listening, alert: chimeAlert))
                        lockTile(sample(.receiverQuiet))
                    }
                    section("When iPhone stops the updates") {
                        lockTile(sample(.signal, ch: 102, db: 30, level: 6, tone: .chime, heard: 50, alert: chimeAlert), stale: true)
                    }
                    Text("Example values. Colours and the badge follow the server's settings, as in the app.")
                        .font(.caption2).foregroundColor(Theme.dim)
                }
                .padding(16)
            }
            .background(Theme.background.ignoresSafeArea())
            .navigationTitle("Live Activity")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
        .preferredColorScheme(.dark)
        .accessibilityIdentifier("live-activity-preview")
    }

    private func section<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title.uppercased()).font(.caption.weight(.semibold)).foregroundColor(Theme.dim)
            content()
        }
    }

    /// The proportions of the tile in the operator's car.
    private func carTile(_ state: LiveTileState) -> some View {
        LiveTileSmall(state: state)
            .frame(width: 250, height: 88)
            .background(RoundedRectangle(cornerRadius: 16).fill(Color(white: 0.11)))
            .clipShape(RoundedRectangle(cornerRadius: 16))
    }

    private func lockTile(_ state: LiveTileState, stale: Bool = false) -> some View {
        LiveTileLockScreen(state: state, isStale: stale)
            .background(RoundedRectangle(cornerRadius: 22).fill(LiveTilePalette.panel))
            .clipShape(RoundedRectangle(cornerRadius: 22))
    }
}

/// How the phone and the car computer reach the server: milliseconds, the network in use, and the 4G stick's signal.
