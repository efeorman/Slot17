import SwiftUI

/// Settings, "Bluetooth to the car computer" (1.4.0): the car computer's switch and name, this phone's link to it,
/// and which route brought matches first. The route is its own observed object, so its changes redraw this card.
struct BluetoothCard: View {
    @ObservedObject var bluetooth: BluetoothRoute
    @State private var name = ""
    @State private var nameProblem: String?

    var body: some View {
        Card(title: "Bluetooth to the car computer") {
            Text("Alerts also come straight from the car computer over Bluetooth: faster, and where there is no internet. With this phone connected, switching off and renaming go over Bluetooth too; switching on goes through the server.")
                .font(.caption2).foregroundColor(Theme.dim)
            Toggle("Bluetooth on the car computer", isOn: Binding(
                get: { bluetooth.shownOn },
                set: { bluetooth.setOnCar($0) }))
                .foregroundColor(Theme.text).tint(Theme.green)
                .disabled((bluetooth.unavailable != nil && !bluetooth.overBluetooth) || bluetooth.carAction.busy)
                .accessibilityIdentifier("setting-bluetooth-car")
            LabelledRow(name: "On the car computer", value: BluetoothWords.car(bluetooth.onCar))
            if let shown = bluetooth.onCar?.name { LabelledRow(name: "Name phones show", value: shown) }
            HStack {
                TextField("New name", text: $name)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .padding(10).background(Theme.panel).foregroundColor(Theme.text)
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                    .onChange(of: name) { _ in
                        if name.count > BluetoothName.maxLength { name = String(name.prefix(BluetoothName.maxLength)) }
                        nameProblem = nil
                    }
                Button("Rename") {
                    nameProblem = bluetooth.rename(name)
                    if nameProblem == nil { name = "" }
                }
                .buttonStyle(ActionButtonStyle())
                .fixedSize()
                .disabled((bluetooth.unavailable != nil && !bluetooth.overBluetooth)
                          || name.trimmingCharacters(in: .whitespaces).isEmpty || bluetooth.carAction.busy)
            }
            if let nameProblem { Text(nameProblem).font(.footnote).foregroundColor(Theme.red) }
            ActionNote(state: bluetooth.carAction)
            if let reason = bluetooth.unavailable { Text(reason).font(.caption).foregroundColor(Theme.dim) }
            Divider().overlay(Theme.line)
            LabelledRow(name: "This phone", value: bluetooth.phoneText)
            if bluetooth.paired {
                Toggle("Use Bluetooth on this phone", isOn: Binding(get: { bluetooth.use }, set: { bluetooth.setUse($0) }))
                    .foregroundColor(Theme.text).tint(Theme.green)
            }
            Button(bluetooth.paired ? "Set up again" : "Set up") { bluetooth.startPairing() }
                .buttonStyle(ActionButtonStyle())
                .disabled(bluetooth.unavailable != nil || bluetooth.phoneUnavailable != nil || bluetooth.pairing.busy)
                .accessibilityIdentifier("bluetooth-set-up")
            if bluetooth.paired {
                Button("Remove from this phone", role: .destructive) { bluetooth.remove() }
                    .buttonStyle(ActionButtonStyle(foreground: Theme.red))
            }
            ActionNote(state: bluetooth.pairing)
            if bluetooth.stats.compared {
                Divider().overlay(Theme.line)
                LabelledRow(name: "First over Bluetooth", value: "\(bluetooth.stats.bluetoothFirst)")
                LabelledRow(name: "First over the server", value: "\(bluetooth.stats.serverFirst)")
                if let lead = bluetooth.stats.medianLead {
                    LabelledRow(name: "Bluetooth ahead by", value: String(format: "%.2f s, median", lead))
                }
                LabelledRow(name: "Only over Bluetooth", value: "\(bluetooth.stats.bluetoothOnly)")
            }
            Text("Setting up needs iOS 18, and the car computer on and nearby. The car computer's key comes through the server, never over Bluetooth. If you swipe the app away, open it again before driving.")
                .font(.caption2).foregroundColor(Theme.dim)
        }
    }
}
