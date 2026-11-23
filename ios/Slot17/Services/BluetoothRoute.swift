import Foundation
import UIKit

/// The Bluetooth route as the app sees it (1.4.0): the car computer's switch and name (requests through the server,
/// like the Pushover switch), this phone's link to it, the frames it brings and how they compare with the server's.
/// AppModel owns one and hands it a few of its services; `deliver` gives new matches to the alert engine with the
/// time they arrived. The link itself needs iOS 18; on older iOS the route only shows why.
final class BluetoothRoute: ObservableObject {
    @Published private(set) var carAction = ActionState()
    @Published private(set) var pairing = ActionState()
    @Published private(set) var wanted: Bool?
    /// Bluetooth on this phone: kept between starts.
    @Published private(set) var use = UserDefaults.standard.object(forKey: "slot17.bluetooth.use") as? Bool ?? true
    private(set) var feed = BluetoothFeed()
    private(set) var stats = RouteStats()
    private var keyID = UserDefaults.standard.string(forKey: "slot17.bluetooth.keyID")
    private var lastRefusalNote = -Double.infinity
    private var link: AnyObject?
    private var pickerWaiting: NSObjectProtocol?

    var sendOp: (String, [String: Any], @escaping (ActionState) -> Void) -> Void = { _, _, _ in }
    var carState: () -> PiState? = { nil }
    var deliver: ([MatchRow], Double) -> Void = { _, _ in }
    var changed: () -> Void = {}
    var note: (String) -> Void = { _ in }

    init() {
        if let hex = Keychain.load(account: Keychain.bluetoothAccount) { feed.key = hexData(hex) }
        if #available(iOS 18.0, *) {
            let link = BluetoothLink()
            link.expectedKeyID = keyID
            link.onFrame = { [weak self] in self?.frame($0) }
            link.onChange = { [weak self] in self?.objectWillChange.send(); self?.changed() }
            link.note = { [weak self] in self?.note($0) }
            self.link = link
        }
    }

    var onCar: BluetoothState? { carState()?.bluetooth }
    var shownOn: Bool { wanted ?? onCar?.on ?? false }

    /// Why the car computer's switch cannot be used, or nil.
    var unavailable: String? {
        guard let state = carState() else { return "The car computer has not reported yet" }
        if !(state.features ?? []).contains("bluetooth") { return "The car computer needs software 1.4.0 or later for this" }
        return nil
    }

    /// Why this phone cannot use Bluetooth, or nil.
    var phoneUnavailable: String? {
        if #available(iOS 18.0, *) { return nil }
        return "Bluetooth to the car computer needs iOS 18 or later"
    }

    // MARK: The car computer

    /// On goes through the server (a link that is off hears nothing); off goes over Bluetooth when it is connected.
    func setOnCar(_ on: Bool) {
        guard unavailable == nil || overBluetooth else { return }
        wanted = on
        note("Bluetooth on the car computer \(on ? "on" : "off") requested")
        if !on, control(.off) { return }
        sendOp("bluetooth", ["on": on]) { [weak self] in self?.carAction = $0 }
    }

    /// Renames the car computer as phones show it: over Bluetooth when connected, else through the server.
    /// Returns why not, or nil when the request went out.
    func rename(_ name: String) -> String? {
        let clean = name.trimmingCharacters(in: .whitespaces)
        guard BluetoothName.valid(clean) else {
            return "Use 1 to \(BluetoothName.maxLength) letters, digits, spaces, '.', '-' or '_'"
        }
        note("Bluetooth name \"\(clean)\" requested")
        if control(.rename, name: clean) { return nil }
        sendOp("bluetooth", ["name": clean]) { [weak self] in self?.carAction = $0 }
        return nil
    }

    /// The link is up and a frame has come from the car computer, so a signed command can go straight to it.
    var overBluetooth: Bool {
        guard #available(iOS 18.0, *), let link = link as? BluetoothLink else { return false }
        return link.state == .connected && feed.heard(Mono.now())
    }

    private func control(_ action: BluetoothFormat.Action, name: String = "") -> Bool {
        guard overBluetooth, let frame = feed.control(action, name: name),
              #available(iOS 18.0, *), let link = link as? BluetoothLink, link.write(frame) else { return false }
        carAction = .init(id: "bluetooth-\(action.rawValue)", text: "Sent over Bluetooth…", level: .idle, busy: true)
        return true
    }

    func settleWanted() {
        guard let wanted, !carAction.busy else { return }
        if onCar?.on == wanted || carAction.level == .bad { self.wanted = nil }
    }

    // MARK: This phone

    /// Step 1 of setting up: the car computer's key, through the server. Step 2 is the system picker.
    func startPairing() {
        guard unavailable == nil, phoneUnavailable == nil else { return }
        guard onCar?.on == true else {
            pairing = .init(text: "Switch Bluetooth on for the car computer first", level: .bad)
            return
        }
        note("Bluetooth set-up started")
        sendOp("bluetooth", ["pair": true]) { [weak self] in self?.pairing = $0 }
    }

    /// The server's answers for this route's requests. The key in a pairing answer is never shown or logged.
    func apply(_ op: OpStatus) {
        if op.id == carAction.id {
            switch op.state {
            case "done": carAction = .init(id: op.id, text: op.detail.isEmpty ? "Saved on the car computer" : op.detail, level: .ok)
            case "failed", "expired": carAction = .init(id: op.id, text: op.detail.isEmpty ? "It did not work" : op.detail, level: .bad)
            default: carAction = .init(id: op.id, text: "Waiting for the car computer…", level: .idle, busy: true)
            }
        } else if op.id == pairing.id, pairing.busy {
            switch op.state {
            case "done": keyArrived(op.id, op.detail)
            case "failed", "expired": pairing = .init(id: op.id, text: op.detail.isEmpty ? "It did not work" : op.detail, level: .bad)
            default: pairing = .init(id: op.id, text: "Waiting for the car computer…", level: .idle, busy: true)
            }
        }
    }

    private func keyArrived(_ id: String, _ detail: String) {
        guard detail.hasPrefix("key:"), let key = hexData(String(detail.dropFirst(4))), key.count == BluetoothFormat.keyBytes else {
            pairing = .init(id: id, text: "The car computer's answer could not be read", level: .bad)
            return
        }
        _ = Keychain.save(BluetoothFormat.hex(key), account: Keychain.bluetoothAccount)
        keyID = BluetoothFormat.keyID(key)
        UserDefaults.standard.set(keyID, forKey: "slot17.bluetooth.keyID")
        feed.key = key
        guard #available(iOS 18.0, *), let link = link as? BluetoothLink else { return }
        link.expectedKeyID = keyID
        setUse(true)
        // Already chosen in the picker: the new key is all it needed (1.5.1). The picker again only for a new car.
        if link.isSetUp && link.state != .wrongCar {
            pairing = .init(id: id, text: "Set up: connecting to the car computer", level: .ok)
            link.reconnect()
            return
        }
        showPickerInForeground(id, link)
    }

    /// iOS shows the picker only to the app on screen: asked while the app was in the background, or while a
    /// system alert covered it, it failed with "Application is not in the foreground". Now it waits for the app.
    @available(iOS 18.0, *)
    private func showPickerInForeground(_ id: String, _ link: BluetoothLink) {
        if let pickerWaiting { NotificationCenter.default.removeObserver(pickerWaiting) }
        pickerWaiting = nil
        guard UIApplication.shared.applicationState == .active else {
            pairing = .init(id: id, text: "Open Slot17 to choose the car computer", level: .idle, busy: true)
            pickerWaiting = NotificationCenter.default.addObserver(forName: UIApplication.didBecomeActiveNotification,
                                                                   object: nil, queue: .main) { [weak self] _ in
                self?.showPickerInForeground(id, link)
            }
            return
        }
        pairing = .init(id: id, text: "Choose the car computer in the list iOS shows…", level: .idle, busy: true)
        link.showPicker { [weak self] why in self?.pairing = .init(id: id, text: why, level: .bad) }
    }

    func setUse(_ on: Bool) {
        use = on
        UserDefaults.standard.set(on, forKey: "slot17.bluetooth.use")
        if on { start() } else { stop() }
    }

    func remove() {
        if #available(iOS 18.0, *), let link = link as? BluetoothLink { link.forget() }
        Keychain.delete(account: Keychain.bluetoothAccount)
        UserDefaults.standard.removeObject(forKey: "slot17.bluetooth.keyID")
        keyID = nil
        feed.key = nil
        pairing = .init(text: "Removed from this phone", level: .ok)
        note("Bluetooth removed from this phone")
    }

    /// While the app listens: keep the link to the car computer, if one is set up and wanted.
    func start() {
        guard use, feed.key != nil else { return }
        if #available(iOS 18.0, *), let link = link as? BluetoothLink { link.start() }
    }

    func stop() {
        if #available(iOS 18.0, *), let link = link as? BluetoothLink { link.stop() }
    }

    // MARK: Frames

    private func frame(_ bytes: Data) {
        let now = Mono.now()
        switch feed.receive(bytes, now: now) {
        case .matches(let keys):
            for key in keys { stats.arrived(key, .bluetooth, at: now) }
            deliver(feed.matches, now)
        case .state:
            changed()
        case .reply(let ok, let text):
            carAction = .init(id: carAction.id, text: text, level: ok ? .ok : .bad)
            note("Bluetooth: the car computer answered: \(text)")
            changed()
        case .refused(let why):
            if now - lastRefusalNote >= 60 {
                lastRefusalNote = now
                note("PROBLEM Bluetooth frame refused: \(why)")
            }
        }
    }

    /// A match from the server, for the route figures.
    func serverMatch(_ key: String, at now: Double) { stats.arrived(key, .server, at: now) }

    func settle(_ now: Double) { stats.settle(now: now) }

    var paired: Bool {
        guard feed.key != nil else { return false }
        if #available(iOS 18.0, *), let link = link as? BluetoothLink { return link.isSetUp }
        return false
    }

    /// This phone's side, in words, for Settings.
    var phoneText: String {
        if let phoneUnavailable { return phoneUnavailable }
        guard #available(iOS 18.0, *), let link = link as? BluetoothLink, paired else { return "Not set up on this phone" }
        if !use { return "Set up, switched off on this phone" }
        let now = Mono.now()
        switch link.state {
        case .connected:
            return feed.heard(now) ? String(format: "Connected, last frame %.0f s ago", now - feed.lastFrameAt) : "Connected"
        case .connecting: return "Waiting for the car computer to be in range"
        case .wrongCar: return "A different car computer answered: set it up again"
        case .bluetoothOff: return "Bluetooth is off on this phone"
        case .notSetUp: return "Not set up on this phone"
        case .off: return "Not connected"
        }
    }
}
