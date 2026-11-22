import SwiftUI
import UIKit

// MARK: Settings

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.openURL) private var openURL
    @State private var editingLink = false
    @State private var confirmRemove = false
    @State private var showingTilePreview = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                ScreenTitle(text: "Settings")
                connection
                quietCard
                sound
                levels
                behaviour
                faultMessages
                BluetoothCard(bluetooth: model.bluetooth)
                liveActivityCard
                locationCard
                serverSettings
                about
            }
            .padding(16)
        }
        .sheet(isPresented: $editingLink) { LinkSheet() }
    }

    private var connection: some View {
        Card(title: "Connection") {
            LabelledRow(name: "Server", value: model.linkHost.ifEmpty("Not set"))
            Button { editingLink = true } label: { Text(model.hasLink ? "Change the private link" : "Add your private link") }
                .buttonStyle(ActionButtonStyle(tint: model.hasLink ? Theme.raised : Theme.blue, foreground: model.hasLink ? Theme.text : .black))
                .accessibilityIdentifier("edit-link")
            if model.hasLink {
                Button { confirmRemove = true } label: { Text("Remove the link from this phone") }
                    .buttonStyle(ActionButtonStyle(foreground: Theme.red))
                    .confirmationDialog("Remove the private link?", isPresented: $confirmRemove, titleVisibility: .visible) {
                        Button("Remove", role: .destructive) { model.removeLink() }
                    }
            }
            Text("This server uses plain HTTP, so the link and everything sent to it, including power off, is not encrypted.")
                .font(.caption2).foregroundColor(Theme.dim)
        }
    }

    private var sound: some View {
        Card(title: "Sound") {
            toggle("Sound", $model.settings.sound, id: "setting-sound")
            toggle(String(format: "Urgent beep from %.0f dB", model.settings.urgentLevel), $model.settings.urgent, id: "setting-urgent")
            Text("Strong signal only. Not police, not a distance, and a quiet radio gives no beep.").font(.caption2).foregroundColor(Theme.dim)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Volume").foregroundColor(Theme.text)
                    Spacer()
                    Text("\(Int(model.settings.volume * 100))%").foregroundColor(Theme.dim).font(.subheadline.monospacedDigit())
                }
                Slider(value: $model.settings.volume, in: 0.1...1, step: 0.05).tint(Theme.green)
                    .accessibilityLabel("Volume")
                    .accessibilityValue("\(Int(model.settings.volume * 100)) percent")
            }
            VStack(alignment: .leading, spacing: 6) {
                Text("With music playing").foregroundColor(Theme.text)
                Picker("With music playing", selection: $model.settings.beepStyle) {
                    ForEach(BeepStyle.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("setting-style")
                Text(styleHint).font(.caption2).foregroundColor(Theme.dim)
            }
            HStack(spacing: 8) {
                Button("Test beep") { model.test(.urgent) }.accessibilityIdentifier("test-urgent")
                Button("Test chime") { model.test(.chime) }.accessibilityIdentifier("test-chime")
                Button("Test warning") { model.test(.linkLost) }
            }
            .buttonStyle(ActionButtonStyle())
            .lineLimit(1).minimumScaleFactor(0.7)
            Text(model.soundLine).font(.caption).foregroundColor(model.soundOK ? Theme.dim : Theme.red)
        }
    }

    private var styleHint: String {
        switch model.settings.beepStyle {
        case .mix: return "The beep plays on top of the music. Nothing is turned down."
        case .duck: return "The music dips for the beep, which adds about half a second."
        case .prompt: return "Like a navigation voice. The music dips and spoken audio may pause. Some cars handle this differently."
        }
    }

    private var levels: some View {
        Card(title: "Levels") {
            level("Red (sound) from", $model.settings.red, 10...45)
            level("Yellow from", $model.settings.yellow, 6...40)
            toggle("Repeat needed", $model.settings.repeatNeeded)
            Text("Chime only if a channel matched twice in a minute. Fewer chimes, but a vehicle that transmits once stays silent. It is not a distance test.")
                .font(.caption2).foregroundColor(Theme.dim)
            Divider().overlay(Theme.line)
            MeterLegend()
        }
    }

    private var behaviour: some View {
        Card(title: "Behaviour") {
            toggle("Warn when the link is lost", $model.settings.linkLostTone)
            Text(String(format: "One tone after %.0f seconds without the car, so silence is not mistaken for nothing nearby.", model.settings.linkLostAfter))
                .font(.caption2).foregroundColor(Theme.dim)
            toggle("Warn when the receiver hears nothing", $model.settings.antennaWarning)
            toggle("Keep the screen on in the dashboard", $model.settings.keepScreenOn)
            VStack(alignment: .leading, spacing: 6) {
                Text("Sleep when closed and no car data").foregroundColor(Theme.text)
                Picker("Sleep after", selection: $model.settings.autoSleepMinutes) {
                    Text("15 min").tag(15)
                    Text("30 min").tag(30)
                    Text("2 h").tag(120)
                    Text("Never").tag(0)
                }
                .pickerStyle(.segmented)
                Text("Saves battery after parking. Once asleep, open the app to arm sound again: iPhone will not let it wake itself.")
                    .font(.caption2).foregroundColor(Theme.dim)
            }
            toggle("Demo mode (fake data)", $model.demo, id: "setting-demo")
        }
    }

    private var faultMessages: some View {
        Card(title: "Fault messages") {
            toggle("Show faults as notifications from this app", $model.settings.faultNotices, id: "setting-fault-notices")
            if model.settings.faultNotices {
                LabelledRow(name: "iPhone permission", value: model.noticePermission)
            }
            Text("The faults Pushover sends: detector degraded, receiver hears nothing, detector stopped, and once when all is well again. Only while this app runs, as it does with sound armed. A closed app sends nothing.")
                .font(.caption2).foregroundColor(Theme.dim)
            Divider().overlay(Theme.line)
            Toggle("Pushover messages from the car computer", isOn: Binding(
                get: { model.pushoverShown },
                set: { model.setPushover($0) }))
                .foregroundColor(Theme.text).tint(Theme.green)
                .disabled(model.pushoverUnavailable != nil || model.pushoverAction.busy)
                .accessibilityIdentifier("setting-pushover")
            if let car = model.pushoverOnCar {
                LabelledRow(name: "On the car computer", value: car.configured == false ? "Pushover not set up" : (car.pushover ? "On" : "Off"))
            }
            if !model.pushoverAction.text.isEmpty { ActionNote(state: model.pushoverAction).accessibilityIdentifier("status-notify") }
            if let reason = model.pushoverUnavailable {
                Text(reason).font(.caption).foregroundColor(Theme.dim)
            }
            Text("Off stops every Pushover message from the car, faults included; switch the notifications above on first so you still hear about faults. A change made while the car is off waits up to a day and applies at the next drive.")
                .font(.caption2).foregroundColor(Theme.dim)
        }
    }

    private var quietCard: some View {
        Card(title: "Quiet mode") {
            toggle("Quiet mode", $model.settings.quiet, id: "setting-quiet")
            Text(String(format: "For busy roads. Chimes only from %.0f dB and at most one per %.0f s. Urgent beeps (%.0f dB and up) stay exactly the same. It may miss or delay a useful chime.",
                        max(model.settings.red, model.settings.quietRed), max(model.settings.anyGap, model.settings.quietGap),
                        model.settings.urgentLevel))
                .font(.caption2).foregroundColor(Theme.dim)
        }
    }

    private var liveActivityCard: some View {
        Card(title: "Lock Screen and CarPlay") {
            toggle("Live Activity", $model.settings.liveActivity, id: "setting-live-activity")
            LabelledRow(name: "Status", value: model.liveTileStatus)
                .accessibilityIdentifier("live-activity-status")
            Button { showingTilePreview = true } label: {
                Label("See how it looks", systemImage: "rectangle.on.rectangle")
            }
            .buttonStyle(ActionButtonStyle())
            .accessibilityIdentifier("live-activity-preview-button")
            Text("Shows the strongest channel and the last beep on the Lock Screen, in the Dynamic Island and on the CarPlay Dashboard (iOS 26). It starts when you open the app and ends 10 minutes after the car is gone. If iPhone stops its updates, it says \"Not updating\" instead of showing old values as new.")
                .font(.caption2).foregroundColor(Theme.dim)
        }
        .sheet(isPresented: $showingTilePreview) { LiveTilePreviewScreen() }
    }

    private var locationCard: some View {
        Card(title: "Location") {
            toggle("Record where alerts and sightings happen", $model.settings.recordLocation, id: "setting-location")
            LabelledRow(name: "Status", value: model.location.statusText)
            Text("Stays on this phone, and goes to your own server with sightings and drive data. Used only to learn which roads and distances the alerts come with. It never changes when a sound plays.")
                .font(.caption2).foregroundColor(Theme.dim)
        }
    }

    private var serverSettings: some View {
        Card(title: "Settings from the server") {
            LabelledRow(name: "In use", value: model.configStatus)
                .accessibilityIdentifier("config-status")
            if !model.settings.overrides.isEmpty {
                LabelledRow(name: "Your own changes", value: model.settings.overrides.sorted().joined(separator: ", "))
                Button { model.useServerSettings() } label: { Text("Use the server settings again") }
                    .buttonStyle(ActionButtonStyle())
                    .accessibilityIdentifier("use-server-settings")
            }
            Button { model.fetchConfig() } label: { Label("Check for new settings", systemImage: "arrow.clockwise") }
                .buttonStyle(ActionButtonStyle())
                .disabled(!model.hasLink || model.demo)
            Text("Sounds, levels, timings and messages can be changed on the server without a new app. Anything you change here stays yours.")
                .font(.caption2).foregroundColor(Theme.dim)
        }
    }

    private var about: some View {
        Card(title: "About") {
            LabelledRow(name: "Version", value: AppModel.version)
            if let release = model.release {
                if let newer = model.appUpdate {
                    Label("App \(newer) is available", systemImage: "arrow.down.circle.fill")
                        .font(.subheadline.weight(.semibold)).foregroundColor(Theme.green)
                        .accessibilityIdentifier("app-update")
                    Text("Download Slot17.ipa from the release and install it as before (README, step 4).")
                        .font(.caption2).foregroundColor(Theme.dim)
                } else if let app = release.app {
                    LabelledRow(name: "Newest release", value: "app \(app), software \(release.software)")
                }
                if let text = release.url, let link = URL(string: text) {
                    Button { openURL(link) } label: { Label("Open the release on GitHub", systemImage: "safari") }
                        .buttonStyle(ActionButtonStyle())
                        .accessibilityIdentifier("open-release")
                }
            }
            Text("This app reports that a TETRA radio transmitted. It cannot identify a radio or vehicle or measure distance, and a silent vehicle is invisible.")
                .font(.caption2).foregroundColor(Theme.dim)
        }
    }

    private func toggle(_ title: String, _ value: Binding<Bool>, id: String? = nil) -> some View {
        Toggle(title, isOn: value).foregroundColor(Theme.text).tint(Theme.green)
            .accessibilityIdentifier(id ?? title)
    }

    private func level(_ title: String, _ value: Binding<Double>, _ range: ClosedRange<Double>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(title).foregroundColor(Theme.text)
                Spacer()
                Text("\(Int(value.wrappedValue)) dB").foregroundColor(Theme.dim).font(.subheadline.monospacedDigit())
            }
            Slider(value: value, in: range, step: 1).tint(Theme.green)
                .accessibilityLabel(title)
                .accessibilityValue("\(Int(value.wrappedValue)) decibels")
        }
    }
}

struct LinkSheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var problem = ""

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 14) {
                Text("Paste the private link you open the web dashboard with. It is kept in this phone's Keychain.")
                    .font(.subheadline).foregroundColor(Theme.dim)
                TextField("http://address:port/?key=...", text: $text, axis: .vertical)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .keyboardType(.URL)
                    .padding(12).background(Theme.panel).foregroundColor(Theme.text)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .accessibilityIdentifier("link-field")
                Button {
                    text = UIPasteboard.general.string ?? ""
                } label: { Label("Paste", systemImage: "doc.on.clipboard") }
                    .buttonStyle(ActionButtonStyle())
                if !problem.isEmpty {
                    Text(problem).font(.footnote).foregroundColor(Theme.red).accessibilityIdentifier("link-problem")
                }
                Spacer()
            }
            .padding(16)
            .background(Theme.background)
            .navigationTitle("Private link")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        if let reason = model.setLink(text) { problem = reason } else { dismiss() }
                    }
                    .accessibilityIdentifier("save-link")
                }
            }
        }
        .preferredColorScheme(.dark)
    }
}

/// Shown first, until there is a private link.
struct SetupCard: View {
    @State private var editing = false
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Connect to your Pi").font(.headline).foregroundColor(Theme.text)
            Text("Add the private link for your display server to start receiving.").font(.subheadline).foregroundColor(Theme.dim)
            Button { editing = true } label: { Text("Add your private link") }
                .buttonStyle(ActionButtonStyle(tint: Theme.blue, foreground: .black))
                .accessibilityIdentifier("setup-link")
            Button { model.demo = true } label: { Text("Try the demo") }
                .buttonStyle(ActionButtonStyle())
        }
        .padding(14)
        .background(Theme.panel)
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .sheet(isPresented: $editing) { LinkSheet() }
    }
}

/// What the meter colours mean, from the current levels and the server's colours and words.
struct MeterLegend: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        let s = model.settings, style = model.style
        VStack(alignment: .leading, spacing: 8) {
            Text("METER COLOURS").font(.caption.weight(.semibold)).foregroundColor(Theme.dim)
            row(Theme.colour(.weak, style), String(format: "Heard, weak (below %.0f dB)", s.yellow))
            row(Theme.colour(.yellow, style), String(format: "Heard, silent (%.0f–%.0f dB)", s.yellow, s.effectiveRed))
            row(Theme.colour(.red, style), String(format: "Chime level (%.0f dB and up)", s.effectiveRed))
            HStack(spacing: 10) {
                RoundedRectangle(cornerRadius: 4).fill(Theme.colour(.red, style))
                    .frame(width: 22, height: 14)
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(Theme.hex(style.strongOutline) ?? .white, lineWidth: 2))
                Text(String(format: "%@: urgent beep level (%.0f dB and up)", style.strongBadge, s.urgentLevel))
                    .font(.footnote).foregroundColor(Theme.text)
            }
            Label(style.frequentText + ": many bursts close together on one channel", systemImage: "waveform")
                .font(.footnote).foregroundColor(Theme.text)
            Text("Colours show what the app does with a signal, not how far away it is or what kind of vehicle. The bar is full only at the urgent level.")
                .font(.caption2).foregroundColor(Theme.dim)
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("meter-legend")
    }

    private func row(_ colour: Color, _ text: String) -> some View {
        HStack(spacing: 10) {
            RoundedRectangle(cornerRadius: 4).fill(colour).frame(width: 22, height: 14)
            Text(text).font(.footnote).foregroundColor(Theme.text)
        }
    }
}

/// The Live Activity's layouts with example values, as they look on CarPlay and the Lock Screen.
/// The same views the widget extension draws; only the surroundings are imitated.
