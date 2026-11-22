import SwiftUI
import UIKit

/// The Pi screen's support package card (1.5.0): make one, then share it, send it to the Slot17 team, or delete it.
/// The route is its own observed object, so its changes redraw this card.
struct SupportCard: View {
    @ObservedObject var support: SupportRoute
    @State private var choice = "quick"
    @State private var session: String?
    @State private var from = ""
    @State private var to = ""
    @State private var recordings = false
    @State private var phoneLog = true
    @State private var places = false
    @State private var askingPlaces = false
    @State private var sending: SupportPackage?
    @State private var deleting: SupportPackage?

    private var window: Result<[Int]?, SupportWords.WindowProblem> { SupportWords.window(from, to) }
    private var windowBad: Bool { if case .failure = window { return true }; return false }
    private var windowValue: [Int]? { if case .success(let w) = window { return w }; return nil }

    var body: some View {
        Card(title: "Support package") {
            Text("One zip with the car computer's logs, its own check and what went wrong, for whoever helps you. Keys, passwords and your private link are left out.")
                .font(.caption2).foregroundColor(Theme.dim)
            Picker("What to include", selection: $choice) {
                Text("Quick").tag("quick")
                Text("One drive").tag("drive")
                Text("Everything").tag("everything")
            }
            .pickerStyle(.segmented)
            Text(choiceHint).font(.caption).foregroundColor(Theme.dim)
            if choice != "quick" { drivePicker }
            Toggle("Include this phone's log", isOn: $phoneLog).foregroundColor(Theme.text).tint(Theme.green)
            Toggle("Include drive data with places", isOn: Binding(
                get: { places },
                set: { on in if on { askingPlaces = true } else { places = false } }))
                .foregroundColor(Theme.text).tint(Theme.green)
            if places {
                Text("Where you drove is in this package. Share it only with someone you trust.").font(.caption).foregroundColor(Theme.red)
            }
            if windowBad { Text(SupportWords.windowHint).font(.caption).foregroundColor(Theme.red) }
            Button {
                support.create(choice: choice, session: session, window: windowValue,
                               recordings: recordings, phoneLog: phoneLog, places: places)
            } label: {
                Label("Create the package", systemImage: "shippingbox")
            }
            .buttonStyle(ActionButtonStyle())
            .disabled(support.unavailable != nil || support.request.busy || windowBad || (choice != "quick" && session == nil))
            .accessibilityIdentifier("support-create")
            ActionNote(state: support.request)
            if let reason = support.unavailable { Text(reason).font(.caption).foregroundColor(Theme.dim) }
            if !support.packages.isEmpty {
                Divider().overlay(Theme.line)
                ForEach(support.packages) { packageRow($0) }
                Text("Your server keeps the newest three. Share saves or sends the file with any app; the web page has a download too.")
                    .font(.caption2).foregroundColor(Theme.dim)
            }
            ActionNote(state: support.fileAction)
        }
        .onAppear { support.refresh() }
        .onChange(of: support.drives) { drives in
            if session == nil || !drives.contains(where: { $0.session == session }) { session = drives.first?.session }
        }
        .sheet(item: Binding(get: { support.share.map(SharedFile.init) }, set: { if $0 == nil { support.share = nil } })) { file in
            ShareSheet(items: [file.url])
        }
        .alert("Include where you drove?", isPresented: $askingPlaces) {
            Button("Include places", role: .destructive) { places = true }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The drive data has every place this phone recorded on the drive, with times. Whoever gets the package can see where you were.")
        }
        .alert("Send to the Slot17 team?", isPresented: Binding(get: { sending != nil }, set: { if !$0 { sending = nil } })) {
            Button("I agree, send it") { if let p = sending { support.sendToTeam(p) }; sending = nil }
            Button("Cancel", role: .cancel) { sending = nil }
        } message: {
            Text("The package goes to the Slot17 team's inbox at support.slot17.cloud. Only the team can read it, and it is deleted after 30 days, or sooner when you take it back here. You get a code to tell them."
                 + (sending?.withPlaces == true ? " It includes where you drove." : ""))
        }
        .alert("Delete this package?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })) {
            Button("Delete", role: .destructive) { if let p = deleting { support.delete(p) }; deleting = nil }
            Button("Cancel", role: .cancel) { deleting = nil }
        } message: {
            Text(deleting?.teamCode != nil ? "It is deleted from your server and from the Slot17 team's inbox." : "It is deleted from your server.")
        }
    }

    private var choiceHint: String {
        switch choice {
        case "quick": return "The last day's logs and the setup check. Small, usually under 1 MB."
        case "drive": return "One drive's matches, faults and logs."
        default: return "One drive with all its detail. Can be large."
        }
    }

    @ViewBuilder private var drivePicker: some View {
        let drives = support.drives
        if drives.isEmpty {
            Text("The car computer has not listed its drives yet.").font(.caption).foregroundColor(Theme.dim)
        } else {
            Picker("Drive", selection: Binding(get: { session ?? drives.first?.session ?? "" }, set: { session = $0 })) {
                ForEach(drives.prefix(8)) { Text(SupportWords.drive($0)).tag($0.session) }
            }
            .pickerStyle(.menu).tint(Theme.text)
            .onAppear { if session == nil { session = drives.first?.session } }
        }
        Text("Only part of it (minutes after the start, optional)").font(.caption).foregroundColor(Theme.dim)
        HStack {
            minutesField("From", text: $from)
            minutesField("To", text: $to)
        }
        if choice == "everything" {
            Toggle("Include the drive's recordings", isOn: $recordings).foregroundColor(Theme.text).tint(Theme.green)
        }
    }

    private func minutesField(_ title: String, text: Binding<String>) -> some View {
        TextField(title, text: text)
            .keyboardType(.numberPad)
            .padding(10).background(Theme.panel).foregroundColor(Theme.text)
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .onChange(of: text.wrappedValue) { v in
                let digits = String(v.filter(\.isNumber).prefix(4))
                if digits != v { text.wrappedValue = digits }
            }
    }

    @ViewBuilder private func packageRow(_ p: SupportPackage) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("\(SupportWords.choice(p.choice)), \(SupportWords.size(p.size)), \(Format.duration(p.ageSeconds)) ago")
                .font(.subheadline.weight(.semibold)).foregroundColor(Theme.text)
            if !p.phone.isEmpty {
                Text("With this phone's " + p.phone.map { $0.contains("places") ? "drive data with places" : "log" }.joined(separator: " and "))
                    .font(.caption).foregroundColor(Theme.dim)
            }
            if let code = p.teamCode {
                Text("Sent to the Slot17 team, code \(code)").font(.footnote).foregroundColor(Theme.green).textSelection(.enabled)
            }
            HStack(spacing: 8) {
                Button { support.shareFile(p) } label: { Label("Share", systemImage: "square.and.arrow.up") }
                    .buttonStyle(ActionButtonStyle())
                if p.teamCode == nil {
                    Button { sending = p } label: { Label("Send to team", systemImage: "paperplane") }
                        .buttonStyle(ActionButtonStyle())
                        .disabled(p.size > SupportWords.teamMaxBytes)
                } else {
                    Button { support.takeBack(p) } label: { Label("Take back", systemImage: "arrow.uturn.backward") }
                        .buttonStyle(ActionButtonStyle())
                }
                Button { deleting = p } label: { Image(systemName: "trash") }
                    .buttonStyle(ActionButtonStyle(foreground: Theme.red))
                    .fixedSize()
                    .accessibilityLabel("Delete")
            }
            .disabled(support.fileAction.busy)
            if p.teamCode == nil && p.size > SupportWords.teamMaxBytes {
                Text("Over 50 MB: too big for the Slot17 team's inbox. Share it instead, or make a smaller one.")
                    .font(.caption).foregroundColor(Theme.dim)
            }
        }
        .padding(.vertical, 4)
    }
}

private struct SharedFile: Identifiable {
    let url: URL
    var id: String { url.path }
}

/// The system share sheet for a file: AirDrop, Mail, Messages, WhatsApp, Save to Files.
private struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
