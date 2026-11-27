import SwiftUI

/// Settings, "Bluetooth to the car computer" (1.4.0): the car computer's switch and name, this phone's link to it,
/// and which route brought matches first. The route is its own observed object, so its changes redraw this card.
struct BluetoothCard: View {
    @ObservedObject var bluetooth: BluetoothRoute
    @State private var name = ""
    @State private var nameProblem: String?
    @State private var typedCode = ""
    @State private var codeProblem: String?
    @State private var confirmNewCode = false

    var body: some View {
        Card(title: "Bluetooth to the car computer") {
            Text("Everything also comes straight from the car computer over Bluetooth: alerts, its state, the Pi screen. With this phone connected, sightings, stop, power off, the Pi log and the other actions go over Bluetooth too, so it all works without internet. Switching Bluetooth on and support packages go through the server; without a server, set Bluetooth up with a pairing code, below.")
                .font(.caption2).foregroundColor(Theme.dim)
            Toggle("Bluetooth on the car computer", isOn: Binding(
                get: { bluetooth.shownOn },
                set: { bluetooth.setOnCar($0) }))
                .foregroundColor(Theme.text).tint(Theme.green)
                .disabled((bluetooth.unavailable != nil && !bluetooth.overBluetooth) || bluetooth.carAction.busy)
                .accessibilityIdentifier("setting-bluetooth-car")
            LabelledRow(name: "On the car computer", value: BluetoothWords.car(bluetooth.onCar))
            if let old = bluetooth.onCarIsOld {
                Text(old).font(.caption2).foregroundColor(Theme.dim)
            }
            if bluetooth.onCar?.phonesPair == true {
                Text("This car computer makes phones confirm a pairing code at every connection. Run the installer once more on it (bash setup.sh car --bluetooth) and it stops; then forget \"\(bluetooth.onCar?.name ?? "Slot17")\" under the iPhone's Settings, Bluetooth, and tap Set up again here: forgetting it there takes it out of this app as well.")
                    .font(.footnote).foregroundColor(Theme.red)
            }
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
                .disabled(bluetooth.setUpUnavailable != nil || bluetooth.pairing.busy)
                .accessibilityIdentifier("bluetooth-set-up")
            if bluetooth.paired {
                Button("Remove from this phone", role: .destructive) { bluetooth.remove() }
                    .buttonStyle(ActionButtonStyle(foreground: Theme.red))
            }
            ActionNote(state: bluetooth.pairing)
            Divider().overlay(Theme.line)
            Text("Without a server").font(.subheadline.weight(.semibold)).foregroundColor(Theme.text)
            Text("This phone and the car computer share a key made from a pairing code. Make one here and write it in slot17.txt on the car computer's card (bluetooth_code=) before its first start, or type it at the setup's question (bash ~/Slot17/setup.sh car --bluetooth). Or type in the code the car computer's setup showed. The code never goes over the air; a new code replaces this phone's key, so the car computer then needs it too.")
                .font(.caption2).foregroundColor(Theme.dim)
            if let code = bluetooth.code {
                Text(code).font(.system(.title3, design: .monospaced)).foregroundColor(Theme.text)
                    .textSelection(.enabled).accessibilityIdentifier("bluetooth-code")
                HStack {
                    Button("Copy") {
                        UIPasteboard.general.setItems([["public.utf8-plain-text": code]],
                                                      options: [.localOnly: true, .expirationDate: Date().addingTimeInterval(600)])
                    }.buttonStyle(ActionButtonStyle())
                    ShareLink(item: code) { Text("Share") }.buttonStyle(ActionButtonStyle())
                }
            }
            Button(bluetooth.code == nil ? "Make a pairing code" : "Make a new pairing code") {
                if bluetooth.code != nil || bluetooth.paired { confirmNewCode = true } else { bluetooth.makeCode() }
            }
                .buttonStyle(ActionButtonStyle())
                .confirmationDialog("Make a new pairing code?", isPresented: $confirmNewCode, titleVisibility: .visible) {
                    Button("Make a new code", role: .destructive) { bluetooth.makeCode() }
                    Button("Cancel", role: .cancel) {}
                } message: {
                    Text("This phone's key is replaced. The car computer and any other phone then need the new code.")
                }
                .disabled(bluetooth.phoneUnavailable != nil || bluetooth.pairing.busy)
                .accessibilityIdentifier("bluetooth-make-code")
            HStack {
                TextField("Code from the car computer", text: $typedCode)
                    .textInputAutocapitalization(.characters).autocorrectionDisabled()
                    .padding(10).background(Theme.panel).foregroundColor(Theme.text)
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                    .onChange(of: typedCode) { _ in codeProblem = nil }
                Button("Use the code") {
                    codeProblem = bluetooth.useCode(typedCode)
                    if codeProblem == nil { typedCode = "" }
                }
                .buttonStyle(ActionButtonStyle()).fixedSize()
                .disabled(typedCode.trimmingCharacters(in: .whitespaces).isEmpty || bluetooth.pairing.busy)
            }
            if let codeProblem { Text(codeProblem).font(.footnote).foregroundColor(Theme.red) }
            if bluetooth.stats.compared {
                Divider().overlay(Theme.line)
                LabelledRow(name: "First over Bluetooth", value: "\(bluetooth.stats.bluetoothFirst)")
                LabelledRow(name: "First over the server", value: "\(bluetooth.stats.serverFirst)")
                if let lead = bluetooth.stats.medianLead {
                    LabelledRow(name: "Bluetooth ahead by", value: String(format: "%.2f s, median", lead))
                }
                LabelledRow(name: "Only over Bluetooth", value: "\(bluetooth.stats.bluetoothOnly)")
            }
            Text("Setting up needs iOS 18, and the car computer on and nearby. The car computer's key comes through the server once, or from a pairing code without one, never over Bluetooth; after that, setting up again needs no internet. If you swipe the app away, open it again before driving.")
                .font(.caption2).foregroundColor(Theme.dim)
        }
    }
}
