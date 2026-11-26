import SwiftUI
import UIKit

// MARK: Pi

struct PiView: View {
    @EnvironmentObject private var model: AppModel
    @State private var confirmingPowerOff = false
    @State private var confirmingStop = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                ScreenTitle(text: "Pi")
                setupCheck
                Card(title: "Connection") {
                    ChainView()
                    NetworkDetails()
                }
                health
                actions
                SupportCard(support: model.support)
                Card(title: "Starting again") {
                    Text("There is no start button, on purpose. The detector starts by itself when the Pi powers up. Repeatedly starting and stopping can jam the receiver.")
                        .font(.footnote).foregroundColor(Theme.dim)
                }
            }
            .padding(16)
        }
        .confirmationDialog("Power off the Pi?", isPresented: $confirmingPowerOff, titleVisibility: .visible) {
            Button("Power off", role: .destructive) { model.requestPowerOff() }
                .accessibilityIdentifier("confirm-power-off")
        } message: {
            Text("This ends the recording session. The Pi comes back only when the ignition goes off and on again.")
        }
        .confirmationDialog("Stop the detector?", isPresented: $confirmingStop, titleVisibility: .visible) {
            Button("Stop the detector", role: .destructive) { model.requestStop() }
                .accessibilityIdentifier("confirm-stop")
        } message: {
            Text("This ends the recording session. It cannot be started from this phone, and after this the app cannot reach the Pi.")
        }
    }

    @ViewBuilder private var setupCheck: some View {
        Card(title: "Setup check") {
            let phone = model.setupRows
            let car = model.carRows
            if let first = SetupCheck.first(phone + car) {
                Text("Start here: \(first.name)").font(.subheadline.weight(.semibold)).foregroundColor(Theme.text)
                if let fix = first.fix { Text(fix).font(.caption).foregroundColor(Theme.dim) }
                Divider().overlay(Theme.line)
            }
            ForEach(phone) { checkLine($0) }
            Divider().overlay(Theme.line)
            Text("Car computer, its own check").font(.footnote.weight(.semibold)).foregroundColor(Theme.dim)
            if let doctor = model.piState?.doctor {
                Text("Checked \(Format.duration(doctor.age)) ago").font(.caption2).foregroundColor(Theme.dim)
            }
            ForEach(car) { checkLine($0) }
            actionBlock(feature: "doctor", state: model.doctorAction) {
                Button { model.requestDoctor() } label: {
                    Label("Check the car computer again", systemImage: "arrow.clockwise")
                }
                .accessibilityIdentifier("check-again")
            }
        }
        .accessibilityIdentifier("setup-check")
    }

    private func checkLine(_ row: CheckRow) -> some View {
        HStack(alignment: .top, spacing: 10) {
            StatusDot(level: row.level, size: 9).padding(.top, 5)
            VStack(alignment: .leading, spacing: 2) {
                Text("\(row.name): \(row.text)").font(.subheadline).foregroundColor(Theme.text)
                if let fix = row.fix, row.level != .ok { Text(fix).font(.caption).foregroundColor(Theme.dim) }
            }
        }
    }

    @ViewBuilder private var health: some View {
        healthCard
    }

    @ViewBuilder private var healthCard: some View {
        Card(title: "Health") {
            if let state = model.piState {
                let pi = state.pi
                let d = state.detector
                if let session = pi?.sessionSeconds { LabelledRow(name: "This session", value: "about \(Format.duration(session))") }
                if let windows = d.windows { LabelledRow(name: "Windows analysed", value: "\(windows)") }
                if let hits = d.hits { LabelledRow(name: "Candidates", value: "\(hits)") }
                if let unchecked = d.unchecked, let partial = d.partial {
                    LabelledRow(name: "Unchecked / partly checked", value: "\(unchecked) / \(partial)")
                }
                if let dropped = pi?.droppedSamples {
                    LabelledRow(name: "Dropped samples", value: String(format: "%.0f", dropped))
                }
                if let peak = d.inputPeak { LabelledRow(name: "Input peak", value: String(format: "%.0f counts", peak)) }
                if let floor = d.floorDB { LabelledRow(name: "Noise floor", value: Format.floor(floor, quiet: model.style.floorQuiet)) }
                if let percent = model.interferencePercent {
                    LabelledRow(name: "Interference, last minute", value: String(format: "%.0f%% of windows", percent))
                }
                if let temperature = pi?.temperature { LabelledRow(name: "Temperature", value: String(format: "%.1f °C", temperature)) }
                if let load = pi?.load { LabelledRow(name: "Load", value: String(format: "%.2f", load)) }
                if let disk = pi?.diskFreeGB { LabelledRow(name: "Disk free", value: String(format: "%.1f GB", disk)) }
                if let memory = pi?.memFreeMB { LabelledRow(name: "Memory free", value: String(format: "%.0f MB", memory)) }
                if let uptime = pi?.uptime { LabelledRow(name: "Pi running for", value: Format.duration(uptime)) }
                if let version = state.software {
                    LabelledRow(name: "Software version", value: version)
                }
                if let newer = model.piUpdate {
                    Label("Software \(newer) is available", systemImage: "arrow.down.circle.fill")
                        .font(.subheadline.weight(.semibold)).foregroundColor(Theme.green)
                        .accessibilityIdentifier("pi-update")
                    Text("Run the installer again on the car computer (README, Updating). With signed updates from your own server it installs by itself.")
                        .font(.caption2).foregroundColor(Theme.dim)
                }
                if let update = state.update, update.state != "off" {
                    LabelledRow(name: "Software", value: Format.software(update))
                }
                if let reasons = d.reasons, !reasons.isEmpty {
                    LabelledRow(name: "Faults", value: reasons.joined(separator: ", "))
                }
                if pi == nil {
                    Text("This Pi has the older publisher, so it sends less detail.").font(.footnote).foregroundColor(Theme.dim)
                }
                Text("Input level is not calibrated and does not show that the antenna works. Session time counts analysed windows.")
                    .font(.caption2).foregroundColor(Theme.dim)
            } else {
                Text("No data from the Pi right now.").font(.subheadline).foregroundColor(Theme.dim)
                    .accessibilityIdentifier("no-pi-data")
            }
        }
    }

    @ViewBuilder private var actions: some View {
        Card(title: "Actions") {
            actionBlock(feature: "mark", state: nil) {
                Button { model.mark(.unknown, note: "saved from the Pi screen", recording: true) } label: {
                    Label("Save a recording now", systemImage: "record.circle")
                }
                .accessibilityIdentifier("save-recording")
            }
            Text("Keeps 3 seconds before and after, on the Pi. At most one per 10 seconds.").font(.caption2).foregroundColor(Theme.dim)
            Divider().overlay(Theme.line)
            actionBlock(feature: "logs", state: model.piLogs) {
                Button { model.requestPiLogs() } label: {
                    Label("Collect the Pi's logs", systemImage: "tray.and.arrow.down")
                }
                .accessibilityIdentifier("collect-pi-logs")
            }
            Divider().overlay(Theme.line)
            let stopReason = model.piState == nil ? "The Pi is not sending data" : nil
            Button { confirmingStop = true } label: {
                Label("Stop the detector", systemImage: "stop.circle")
            }
            .buttonStyle(ActionButtonStyle())
            .disabled(stopReason != nil || model.demo)
            .accessibilityIdentifier("stop-detector")
            ActionNote(state: model.stopAction).accessibilityIdentifier("stop-status")
            if let stopReason { reasonText(stopReason) }
            Divider().overlay(Theme.line)
            actionBlock(feature: "poweroff", state: model.powerOff, tint: Theme.red.opacity(0.85), foreground: .white) {
                Button { confirmingPowerOff = true } label: {
                    Label("Power off the Pi", systemImage: "power")
                }
                .accessibilityIdentifier("power-off")
            }
        }
    }

    @ViewBuilder
    private func actionBlock<B: View>(feature: String, state: ActionState?, tint: Color = Theme.raised,
                                      foreground: Color = Theme.text, @ViewBuilder button: () -> B) -> some View {
        let reason = model.unavailable(feature)
        button()
            .buttonStyle(ActionButtonStyle(tint: tint, foreground: foreground))
            .disabled(reason != nil || state?.busy == true)
        if let state { ActionNote(state: state).accessibilityIdentifier("status-\(feature)") }
        if let reason { reasonText(reason) }
    }

    private func reasonText(_ text: String) -> some View {
        Text(text).font(.caption).foregroundColor(Theme.dim)
    }
}
