import SwiftUI

enum Theme {
    static let background = Color(red: 0.043, green: 0.055, blue: 0.075)
    static let panel = Color(red: 0.075, green: 0.094, blue: 0.129)
    static let raised = Color(red: 0.106, green: 0.133, blue: 0.176)
    static let line = Color(red: 0.137, green: 0.169, blue: 0.220)
    static let text = Color(red: 0.91, green: 0.93, blue: 0.957)
    static let dim = Color(red: 0.60, green: 0.65, blue: 0.71)
    static let green = Color(red: 0.184, green: 0.824, blue: 0.42)
    static let yellow = Color(red: 0.961, green: 0.773, blue: 0.259)
    static let red = Color(red: 1.0, green: 0.302, blue: 0.31)
    static let grey = Color(red: 0.227, green: 0.267, blue: 0.322)
    static let blue = Color(red: 0.306, green: 0.639, blue: 1.0)
    static let segmentOff = Color(red: 0.106, green: 0.133, blue: 0.176)

    static func colour(_ level: Level) -> Color {
        switch level {
        case .ok: return green
        case .warn: return yellow
        case .bad: return red
        case .idle: return grey
        }
    }

    static func colour(_ meter: MeterColour, _ style: DisplayStyle = DisplayStyle()) -> Color {
        switch meter {
        case .weak: return hex(style.weak) ?? grey
        case .yellow: return hex(style.silent) ?? yellow
        case .red: return hex(style.chime) ?? red
        case .grey: return grey
        }
    }

    static func hex(_ value: String) -> Color? {
        guard let (r, g, b) = HexColour.rgb(value) else { return nil }
        return Color(red: r, green: g, blue: b)
    }
}

struct Card<Content: View>: View {
    var title: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let title {
                Text(title).font(.footnote.weight(.semibold)).foregroundColor(Theme.dim).textCase(.uppercase)
            }
            content
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.panel)
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(Theme.line, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 16))
    }
}

/// A title with room for the screen's content, used by every screen except the live one.
struct ScreenTitle: View {
    let text: String
    var body: some View {
        Text(text).font(.largeTitle.bold()).foregroundColor(Theme.text)
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityAddTraits(.isHeader)
    }
}

struct StatusDot: View {
    let level: Level
    var size: CGFloat = 10
    var body: some View {
        Circle().fill(Theme.colour(level)).frame(width: size, height: size)
            .shadow(color: level == .idle ? .clear : Theme.colour(level).opacity(0.6), radius: 4)
    }
}

extension View {
    /// The surface of a button. Built with the iOS 26 SDK and running on iOS 26: Liquid Glass, Apple's
    /// current look for controls, tinted with the button's colour when it has one. Otherwise, and
    /// whenever `glass` is false, the solid colour as before. Only controls use it: the meters, cards and
    /// text keep solid backgrounds, because reading them at a glance in a car comes first.
    ///
    /// The glass is only a picture behind the button: it never takes a touch, and the whole rounded shape is
    /// the button's tap area. App 1.6.0 put interactive glass on the button itself, and on the operator's
    /// iPhone the buttons stopped answering (18 November 2026): the glass either took the touch or left
    /// only the icon and words tappable. The simulator test tapped the exact centre and missed it.
    func controlSurface(_ fill: Color, cornerRadius: CGFloat, tinted: Bool = false, glass: Bool = true) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius)
        return surface(fill, shape: shape, tinted: tinted, glass: glass).contentShape(shape)
    }

    @ViewBuilder
    private func surface(_ fill: Color, shape: RoundedRectangle, tinted: Bool, glass: Bool) -> some View {
        #if compiler(>=6.2)
        if glass {
            if #available(iOS 26.0, *) {
                self.background {
                    shape.fill(Color.clear)
                        .glassEffect(tinted ? Glass.regular.tint(fill) : Glass.regular, in: shape)
                        .allowsHitTesting(false)
                }
            } else {
                self.background(fill).clipShape(shape)
            }
        } else {
            self.background(fill).clipShape(shape)
        }
        #else
        self.background(fill).clipShape(shape)
        #endif
    }
}

struct ActionButtonStyle: ButtonStyle {
    var tint: Color = Theme.raised
    var foreground: Color = Theme.text
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.semibold))
            .foregroundColor(isEnabled ? foreground : Theme.dim)
            .frame(maxWidth: .infinity, minHeight: 46)
            .padding(.horizontal, 10)
            .controlSurface(isEnabled ? tint : Theme.raised.opacity(0.5), cornerRadius: 12,
                            tinted: tint != Theme.raised, glass: isEnabled)
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .opacity(configuration.isPressed ? 0.85 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// Presses in quickly and springs back, so a tap feels answered at once.
struct PressableStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.95 : 1)
            .brightness(configuration.isPressed ? 0.06 : 0)
            .animation(reduceMotion ? nil : .spring(response: 0.25, dampingFraction: 0.65), value: configuration.isPressed)
    }
}

struct LabelledRow: View {
    let name: String
    let value: String
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(name).foregroundColor(Theme.dim)
            Spacer(minLength: 8)
            Text(value).foregroundColor(Theme.text).multilineTextAlignment(.trailing)
                .font(.body.monospacedDigit())
        }
        .font(.subheadline)
        .accessibilityElement(children: .combine)
    }
}

/// What became of a request, shown under its button.
struct ActionNote: View {
    let state: ActionState
    var body: some View {
        if !state.text.isEmpty {
            HStack(alignment: .top, spacing: 8) {
                if state.busy {
                    ProgressView().controlSize(.small).tint(Theme.dim)
                } else {
                    StatusDot(level: state.level, size: 8).padding(.top, 5)
                }
                Text(state.text).font(.footnote).foregroundColor(state.level == .bad ? Theme.red : Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
