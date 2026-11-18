import SwiftUI
import WidgetKit

struct MarkEntry: TimelineEntry {
    let date: Date
    var look = MarkWidgetModel.Look(tickedKind: nil, line: nil)
}

/// The buttons never change; the last tap is read from the App Group (when the signing kept one), and
/// the widget redraws by itself when its tick and its line expire. A tap reloads it at once.
struct MarkProvider: TimelineProvider {
    func placeholder(in context: Context) -> MarkEntry { MarkEntry(date: Date()) }
    func getSnapshot(in context: Context, completion: @escaping (MarkEntry) -> Void) {
        let now = Date()
        completion(MarkEntry(date: now, look: MarkWidgetModel.look(SharedMarks.load(), at: now)))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<MarkEntry>) -> Void) {
        let last = SharedMarks.load()
        let entries = MarkWidgetModel.changes(last, from: Date()).map { MarkEntry(date: $0, look: MarkWidgetModel.look(last, at: $0)) }
        completion(Timeline(entries: entries, policy: .never))
    }
}

/// On a touchscreen car, widget buttons work without the app being a CarPlay app (Apple, "Adding StandBy
/// and CarPlay support to your widget"). `systemSmall` is all CarPlay asks for. The widget lives on the
/// page left of the Dashboard; the Live Activity next to the map cannot have buttons.
struct Slot17MarkWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "Slot17Mark", provider: MarkProvider()) { entry in
            MarkWidgetFamilyView(look: entry.look)
        }
        .configurationDisplayName("Slot17 sightings")
        .description("Mark Police or Saw nothing without opening the app.")
        // The Lock Screen sizes are for a tap without unlocking. CarPlay shows only systemSmall.
        .supportedFamilies([.systemSmall, .accessoryCircular, .accessoryRectangular])
        .contentMarginsDisabled()
    }
}

/// The Home Screen and CarPlay get the two big buttons; the Lock Screen gets the same taps, smaller.
/// Lock Screen widgets are drawn in one tint by iOS, so they rely on symbols and words, not colour.
private struct MarkWidgetFamilyView: View {
    let look: MarkWidgetModel.Look
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .accessoryCircular:
            Button(intent: MarkSightingIntent(kind: "police")) {
                VStack(spacing: 1) {
                    Image(systemName: look.tickedKind == "police" ? "checkmark.circle.fill" : "light.beacon.max.fill")
                        .font(.system(size: 20, weight: .semibold))
                    Text("Police").font(.system(size: 10, weight: .semibold)).lineLimit(1).minimumScaleFactor(0.7)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("I saw: police")
            .containerBackground(for: .widget) { AccessoryWidgetBackground() }
        case .accessoryRectangular:
            HStack(spacing: 6) {
                accessoryButton(kind: "police", title: "Police", symbol: "light.beacon.max.fill")
                accessoryButton(kind: "noise", title: "Nothing", symbol: "eye.slash.fill")
            }
            .containerBackground(for: .widget) { Color.clear }
        default:
            MarkWidgetContent(look: look)
                .padding(8)
                .containerBackground(LiveTilePalette.background, for: .widget)
        }
    }

    private func accessoryButton(kind: String, title: String, symbol: String) -> some View {
        Button(intent: MarkSightingIntent(kind: kind)) {
            VStack(spacing: 2) {
                Image(systemName: look.tickedKind == kind ? "checkmark.circle.fill" : symbol)
                    .font(.system(size: 17, weight: .semibold))
                Text(title).font(.system(size: 11, weight: .semibold)).lineLimit(1).minimumScaleFactor(0.7)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(RoundedRectangle(cornerRadius: 10).fill(.white.opacity(0.18)))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(kind == "police" ? "I saw: police" : "I saw nothing")
    }
}

/// Control Center, the Lock Screen's control buttons and the Action button (iOS 18): one tap marks a
/// sighting, through the same intent as the widget, which runs in the app without opening it.
struct Slot17PoliceControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "nl.slot17.app.control.police") {
            ControlWidgetButton(action: MarkSightingIntent(kind: "police")) {
                Label("Saw police", systemImage: "light.beacon.max.fill")
            }
        }
        .displayName("Slot17: saw police")
        .description("Marks a police sighting, with the time and place, without opening the app.")
    }
}

struct Slot17NothingControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "nl.slot17.app.control.nothing") {
            ControlWidgetButton(action: MarkSightingIntent(kind: "noise")) {
                Label("Saw nothing", systemImage: "eye.slash.fill")
            }
        }
        .displayName("Slot17: saw nothing")
        .description("Marks that nothing was in sight after a beep, without opening the app.")
    }
}
