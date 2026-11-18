import AppIntents
import SwiftUI

/// A tap on the widget's Police or Saw nothing button. As a LiveActivityIntent it runs in the app's
/// own process without opening the app (Apple, "Adding interactivity to widgets and Live Activities"),
/// which is where the sightings, the location and the sound are. The extension only needs to know the
/// type, so no App Group is needed, and free signing has nothing extra to get right.
@available(iOS 17.0, *)
struct MarkSightingIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Mark a sighting"
    static var isDiscoverable = false
    static var openAppWhenRun = false
    static var authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @Parameter(title: "Kind") var kind: String

    init() {}
    init(kind: String) { self.kind = kind }

    func perform() async throws -> some IntentResult {
        #if SLOT17_APP
        let tappedAt = Date()
        await MainActor.run { WidgetMarks.record(kind: kind, tappedAt: tappedAt) }
        #endif
        return .result()
    }
}

/// Two big buttons, for the CarPlay widget page and the Home Screen. CarPlay scales small widgets up,
/// shows them in full colour and removes the background, so the buttons carry their own.
@available(iOS 17.0, *)
struct MarkWidgetContent: View {
    /// What the last tap left: a tick on its button for a minute, a line for half an hour. Empty when
    /// the signing kept no App Group.
    var look = MarkWidgetModel.Look(tickedKind: nil, line: nil)

    var body: some View {
        VStack(spacing: 6) {
            button(kind: "police", title: "Saw police", symbol: "light.beacon.max.fill",
                   tint: Color(red: 0.29, green: 0.56, blue: 0.96))
            button(kind: "noise", title: "Saw nothing", symbol: "eye.slash.fill",
                   tint: Color(red: 0.36, green: 0.40, blue: 0.47))
            if let line = look.line {
                HStack(spacing: 4) {
                    Image(systemName: "checkmark.circle.fill").foregroundColor(.green)
                    Text(line.title).fontWeight(.semibold).lineLimit(1).minimumScaleFactor(0.7)
                    LiveTileClock(since: line.at).foregroundColor(LiveTilePalette.dim)
                }
                .font(.system(size: 12, weight: .medium, design: .rounded))
                .foregroundColor(LiveTilePalette.text)
                .frame(maxWidth: .infinity)
                .accessibilityIdentifier("widget-last-mark")
            }
        }
    }

    private func button(kind: String, title: String, symbol: String, tint: Color) -> some View {
        Button(intent: MarkSightingIntent(kind: kind)) {
            HStack(spacing: 6) {
                Image(systemName: look.tickedKind == kind ? "checkmark.circle.fill" : symbol)
                    .font(.system(size: 16, weight: .semibold))
                Text(title).font(.system(size: 15, weight: .bold, design: .rounded))
                    .lineLimit(1).minimumScaleFactor(0.7)
            }
            .foregroundColor(.white)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(RoundedRectangle(cornerRadius: 14).fill(tint))
            .contentShape(RoundedRectangle(cornerRadius: 14))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("I saw: \(title)")
        .accessibilityIdentifier("widget-mark-\(kind)")
    }
}
