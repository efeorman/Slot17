import SwiftUI
import UIKit

// MARK: Logs

struct LogsView: View {
    enum Section: String, CaseIterable, Identifiable {
        case alerts = "Alerts", diagnostics = "Diagnostics", server = "On server"
        var id: String { rawValue }
    }

    @EnvironmentObject private var model: AppModel
    @State private var section = Section.alerts
    @State private var soundedOnly = false
    @State private var confirmClear = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                ScreenTitle(text: "Logs")
                Picker("Show", selection: $section) {
                    ForEach(Section.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("logs-section")
                switch section {
                case .alerts: alerts
                case .diagnostics: diagnostics
                case .server: server
                }
            }
            .padding(16)
        }
        .onChange(of: section) { if $0 == .server { model.refreshServerLogs() } }
    }

    @ViewBuilder private var alerts: some View {
        Toggle("Only alerts that made a sound", isOn: $soundedOnly).font(.subheadline).foregroundColor(Theme.text).tint(Theme.green)
        let rows = soundedOnly ? model.alerts.filter { $0.sound != nil } : model.alerts
        if rows.isEmpty {
            Card(title: nil) { Text("No recognised signals yet.").font(.subheadline).foregroundColor(Theme.dim) }
        }
        ForEach(rows.prefix(200)) { alert in
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    if Aircraft.isAir(alert.channel) {
                        HelicopterGlyph().fill(alert.sound == nil ? Theme.dim : Aircraft.colour).frame(width: 17, height: 17)
                    } else {
                        Image(systemName: alert.sound == nil ? "bell.slash" : "bell.fill")
                            .foregroundColor(alert.sound == .urgent ? Theme.red : (alert.sound == nil ? Theme.dim : Theme.yellow))
                    }
                    Text(AlertText.title(alert)).font(.subheadline.weight(.semibold)).foregroundColor(Theme.text)
                        .accessibilityIdentifier("alert-row")
                    Spacer()
                    Text(alert.at, format: .dateTime.hour().minute().second()).font(.caption.monospacedDigit()).foregroundColor(Theme.dim)
                }
                if !alert.reason.isEmpty {
                    Text(alert.reason).font(.footnote).foregroundColor(Theme.dim)
                }
                if let rule = alert.rule {
                    // How the word was found, not how close or who: say so where it is shown.
                    Text("Recognised by the \(rule) rule").font(.caption).foregroundColor(Theme.dim)
                        .accessibilityIdentifier("alert-rule")
                }
            }
            .padding(10)
            .background(Theme.panel)
            .clipShape(RoundedRectangle(cornerRadius: 12))
        }
    }

    @ViewBuilder private var diagnostics: some View {
        Button { model.uploadAppLog() } label: {
            Label("Send this log to the server", systemImage: "paperplane.fill")
        }
        .buttonStyle(ActionButtonStyle(tint: Theme.blue, foreground: .black))
        .disabled(!model.hasLink || model.logUpload.busy)
        .accessibilityIdentifier("send-app-log")
        ActionNote(state: model.logUpload).accessibilityIdentifier("log-upload-status")
        Button { model.uploadDriveData() } label: {
            Label("Send drive data (alerts, sightings, places)", systemImage: "map.fill")
        }
        .buttonStyle(ActionButtonStyle())
        .disabled(!model.hasLink || model.driveUpload.busy)
        .accessibilityIdentifier("send-drive-data")
        ActionNote(state: model.driveUpload).accessibilityIdentifier("drive-upload-status")
        Text("Also sent by itself a few minutes after each drive, or the next time you open the app.").font(.caption2).foregroundColor(Theme.dim)
        ShareLink(item: model.log.url) { Label("Share this log", systemImage: "square.and.arrow.up") }
            .buttonStyle(ActionButtonStyle())
        Card(title: "Now") {
            ForEach(Array(model.diagnostics.enumerated()), id: \.offset) { _, row in
                LabelledRow(name: row.0, value: row.1)
            }
        }
        Card(title: "Recent events") {
            ForEach(Array(model.logLines.suffix(60).reversed().enumerated()), id: \.offset) { _, line in
                Text(line).font(.system(.caption2, design: .monospaced))
                    .foregroundColor(line.contains("PROBLEM") ? Theme.red : Theme.dim)
                    .textSelection(.enabled)
            }
        }
        Button(role: .destructive) { confirmClear = true } label: { Text("Clear this log") }
            .buttonStyle(ActionButtonStyle(foreground: Theme.red))
            .confirmationDialog("Delete this log from the phone?", isPresented: $confirmClear, titleVisibility: .visible) {
                Button("Delete log", role: .destructive) { model.clearLocalLog() }
            }
    }

    @ViewBuilder private var server: some View {
        if !model.phonePiLogs.isEmpty {
            Text("From the Pi over Bluetooth, kept on this phone.").font(.footnote).foregroundColor(Theme.dim)
            ForEach(model.phonePiLogs) { log in
                NavigationLink { PhonePiLogText(log: log) } label: {
                    HStack {
                        Image(systemName: "antenna.radiowaves.left.and.right").foregroundColor(Theme.blue).frame(width: 24)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("From the Pi, over Bluetooth").font(.subheadline.weight(.semibold)).foregroundColor(Theme.text)
                            Text("\(Format.ago(Date().timeIntervalSince(log.at))) · \(log.size / 1024 + 1) KB").font(.caption).foregroundColor(Theme.dim)
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.caption).foregroundColor(Theme.dim)
                    }
                    .padding(10)
                    .background(Theme.panel)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                }
                .accessibilityIdentifier("phone-pi-log-row")
            }
        }
        Text("Logs sent from this app and collected from the Pi, kept on the display server.")
            .font(.footnote).foregroundColor(Theme.dim)
        Button { model.refreshServerLogs() } label: { Label("Refresh", systemImage: "arrow.clockwise") }
            .buttonStyle(ActionButtonStyle())
            .disabled(!model.hasLink)
        if !model.serverLogsNote.isEmpty {
            Text(model.serverLogsNote).font(.footnote).foregroundColor(Theme.dim)
        }
        ForEach(model.serverLogs) { entry in
            NavigationLink { ServerLogText(name: entry.name) } label: {
                HStack {
                    Image(systemName: entry.name.hasPrefix("pi-") ? "cpu" : "iphone").foregroundColor(Theme.blue).frame(width: 24)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(entry.name.hasPrefix("pi-") ? "From the Pi" : "From this app").font(.subheadline.weight(.semibold)).foregroundColor(Theme.text)
                        Text("\(Format.ago(entry.ageSeconds)) · \(entry.size / 1024 + 1) KB").font(.caption).foregroundColor(Theme.dim)
                    }
                    Spacer()
                    Image(systemName: "chevron.right").font(.caption).foregroundColor(Theme.dim)
                }
                .padding(10)
                .background(Theme.panel)
                .clipShape(RoundedRectangle(cornerRadius: 12))
            }
            .accessibilityIdentifier("server-log-row")
        }
    }
}

/// A Pi log that came over Bluetooth (1.6.0), from the file on this phone.
private struct PhonePiLogText: View {
    let log: AppModel.PhoneLog
    @EnvironmentObject private var model: AppModel
    @State private var text = "Loading…"

    var body: some View {
        ScrollView {
            Text(text).font(.system(.caption2, design: .monospaced)).foregroundColor(Theme.text)
                .textSelection(.enabled).padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.background)
        .navigationTitle("From the Pi, over Bluetooth")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ShareLink(item: log.url) }
        .onAppear { text = String(model.readPhonePiLog(log).prefix(200_000)) }
    }
}

private struct ServerLogText: View {
    let name: String
    @EnvironmentObject private var model: AppModel
    @State private var text = "Loading…"

    var body: some View {
        ScrollView {
            Text(text).font(.system(.caption2, design: .monospaced)).foregroundColor(Theme.text)
                .textSelection(.enabled).padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityIdentifier("server-log-text")
        }
        .background(Theme.background)
        .navigationTitle(name.hasPrefix("pi-") ? "From the Pi" : "From this app")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ShareLink(item: text) }
        .onAppear { model.fetchServerLog(name) { text = String($0.prefix(200_000)) } }
    }
}
