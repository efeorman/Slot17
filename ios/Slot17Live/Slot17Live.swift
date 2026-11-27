import ActivityKit
import SwiftUI
import WidgetKit

@main
struct Slot17LiveBundle: WidgetBundle {
    var body: some Widget {
        Slot17LiveActivity()
        Slot17MarkWidget()
        Slot17PoliceControl()
        Slot17NothingControl()
    }
}

/// The Live Activity. iOS 26 forwards it to the CarPlay Dashboard by itself; the `.small` family
/// gives CarPlay (and Apple Watch) a layout made for it instead of the Dynamic Island's compact
/// pieces (Apple, WWDC25 "What's new in widgets"; CarPlay Developer Guide, October 2026).
struct Slot17LiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: SlotActivityAttributes.self) { context in
            Presentation(state: context.state, isStale: context.isStale)
                .activityBackgroundTint(LiveTilePalette.background.opacity(0.92))
                .activitySystemActionForegroundColor(LiveTilePalette.text)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    LiveTileMeter(state: context.state, spacing: 2, dimmed: context.isStale)
                        .frame(width: 16, height: 56)
                        .padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(spacing: 2) {
                        HStack(spacing: 6) {
                            if context.state.air && !context.isStale {
                                HelicopterGlyph().fill(Aircraft.colour).frame(width: 17, height: 17)
                            }
                            Text(context.state.phaseTitle).font(.headline)
                                .contentTransition(.numericText())
                            if context.state.tone == .strong, !context.isStale { LiveTileBadge(state: context.state, small: true) }
                        }
                        if let strength = context.state.strength, context.state.phase == .signal {
                            Text("\(strength) dB").font(.subheadline.weight(.semibold))
                                .foregroundColor(context.isStale ? LiveTilePalette.dim : context.state.toneColour)
                                .contentTransition(.numericText())
                        } else {
                            Text(context.state.phaseDetail).font(.caption).foregroundColor(LiveTilePalette.dim)
                        }
                    }
                    .lineLimit(1)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    if let heard = context.state.heardAt, context.state.phase == .signal {
                        LiveTileClock(since: heard, alignment: .trailing).font(.subheadline.weight(.semibold))
                            .foregroundColor(LiveTilePalette.dim)
                            .frame(width: 52, alignment: .trailing)
                            .padding(.trailing, 4)
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    if context.isStale {
                        Label("Not updating · open Slot17", systemImage: "clock.badge.exclamationmark")
                            .font(.caption.weight(.semibold)).foregroundColor(LiveTilePalette.caution)
                    } else if let alert = context.state.lastAlert {
                        HStack(spacing: 5) {
                            if Aircraft.isAir(alert.channel) {
                                HelicopterGlyph().fill(Aircraft.colour).frame(width: 14, height: 14)
                            } else {
                                Image(systemName: "bell.fill")
                            }
                            Text(alert.urgent ? "Urgent" : "Chime").fontWeight(.semibold)
                            Text("L\(alert.channel)")
                            Spacer(minLength: 4)
                            LiveTileClock(since: alert.at, alignment: .trailing).frame(width: 48, alignment: .trailing)
                        }
                        .font(.caption)
                        .foregroundColor(LiveTilePalette.dim)
                    }
                }
            } compactLeading: {
                LiveTileIslandMark(state: context.state, isStale: context.isStale)
            } compactTrailing: {
                LiveTileIslandValue(state: context.state, isStale: context.isStale)
            } minimal: {
                LiveTileIslandMark(state: context.state, isStale: context.isStale)
            }
            .keylineTint(context.state.tone == .strong ? context.state.outlineColour : context.state.toneColour)
        }
        .supplementalActivityFamilies([.small])
    }
}

/// Picks the layout for where the activity is shown: `.small` on CarPlay and Apple Watch,
/// `.medium` on the iPhone Lock Screen.
private struct Presentation: View {
    let state: LiveTileState
    let isStale: Bool
    @Environment(\.activityFamily) private var family
    @Environment(\.isLuminanceReduced) private var reduced

    var body: some View {
        switch family {
        case .small:
            LiveTileSmall(state: state, isStale: isStale, reduced: reduced)
        default:
            LiveTileLockScreen(state: state, isStale: isStale, reduced: reduced)
        }
    }
}
