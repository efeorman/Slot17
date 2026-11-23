import AccessorySetupKit
import CoreBluetooth
import UIKit

/// The phone's side of the car computer's Bluetooth link (docs/design/BLUETOOTH.md), iOS 18 and later.
///
/// AccessorySetupKit shows the system picker once and gives the app this one car computer; from iOS 26 that is
/// also what lets iOS relaunch the app for a Bluetooth event (Apple TN3115). After that it is plain Core
/// Bluetooth: a connection request that never times out, so iOS connects whenever the car computer is in range,
/// with state restoration for when iOS ended the app in the background. Everything arrives on the main queue.
@available(iOS 18.0, *)
final class BluetoothLink: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    enum State: Equatable { case off, notSetUp, connecting, connected, wrongCar, bluetoothOff }

    private(set) var state: State = .notSetUp { didSet { if state != oldValue { onChange() } } }
    private(set) var mtu: Int?
    var onFrame: (Data) -> Void = { _ in }
    var onChange: () -> Void = {}
    var note: (String) -> Void = { _ in }
    /// Which car computer is expected: the key id the server gave with the key.
    var expectedKeyID: String?

    private let session = ASAccessorySession()
    private var central: CBCentralManager?
    private var peripheral: CBPeripheral?
    private var accessory: ASAccessory?
    private var wanted = false
    private static let service = CBUUID(string: BluetoothFormat.serviceUUID)
    private static let frames = CBUUID(string: BluetoothFormat.framesUUID)
    private static let info = CBUUID(string: BluetoothFormat.infoUUID)
    private static let control = CBUUID(string: BluetoothFormat.controlUUID)
    private var controlCharacteristic: CBCharacteristic?
    private var lastProblemNote = -Double.infinity
    /// Seconds before a link that went wrong is tried again: long enough not to hammer, short enough to feel live.
    static let retryAfter: TimeInterval = 4

    override init() {
        super.init()
        session.activate(on: .main) { [weak self] event in self?.handle(event) }
    }

    /// True once this phone has been given a car computer through the picker.
    var isSetUp: Bool { accessory != nil }

    /// The system picker, for car computers that advertise the Slot17 service.
    func showPicker(failed: @escaping (String) -> Void) {
        let descriptor = ASDiscoveryDescriptor()
        descriptor.bluetoothServiceUUID = Self.service
        let item = ASPickerDisplayItem(name: "Slot17 car computer",
                                       productImage: UIImage(systemName: "car.fill") ?? UIImage(), descriptor: descriptor)
        session.showPicker(for: [item]) { error in
            if let error { DispatchQueue.main.async { failed(error.localizedDescription) } }
        }
    }

    /// Keep connecting while the app listens and Bluetooth is wanted on this phone.
    func start() {
        wanted = true
        connectIfReady()
    }

    func stop() {
        wanted = false
        if let peripheral { central?.cancelPeripheralConnection(peripheral) }
        state = accessory == nil ? .notSetUp : .off
    }

    /// Connects again from the start, for a new key or after "Set up again", without the picker.
    func reconnect() {
        wanted = true
        state = accessory == nil ? .notSetUp : .connecting
        if let peripheral, peripheral.state != .disconnected {
            central?.cancelPeripheralConnection(peripheral)      // didDisconnect connects again
        } else {
            connectIfReady()
        }
    }

    /// Something went wrong between connecting and the first frame: say so (once a minute at most), drop the
    /// connection and let didDisconnect connect again a moment later. Before 1.5.1 the link stayed connected and
    /// silent, which the app showed as no data from Bluetooth.
    private func retry(_ peripheral: CBPeripheral, _ why: String) {
        let now = Mono.now()
        if now - lastProblemNote >= 60 {
            lastProblemNote = now
            note("PROBLEM Bluetooth: \(why); connecting again")
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.retryAfter) { [weak self] in
            guard let self, self.wanted, self.state != .wrongCar, peripheral.state != .disconnected else { return }
            self.central?.cancelPeripheralConnection(peripheral)
        }
    }

    /// Writes a control frame (switch off, rename) to the car computer. False when there is no link to write on.
    func write(_ data: Data) -> Bool {
        guard state == .connected, let peripheral, let controlCharacteristic else { return false }
        peripheral.writeValue(data, for: controlCharacteristic, type: .withResponse)
        return true
    }

    /// Removes the car computer from this app, through AccessorySetupKit.
    func forget() {
        stop()
        if let accessory { session.removeAccessory(accessory) { _ in } }
    }

    private func handle(_ event: ASAccessoryEvent) {
        switch event.eventType {
        case .activated:
            accessory = session.accessories.first
            connectIfReady()
        case .accessoryAdded:
            accessory = event.accessory ?? session.accessories.first
            note("Bluetooth: the car computer was added")
            connectIfReady()
        case .accessoryRemoved:
            accessory = nil
            peripheral = nil
            state = .notSetUp
        default:
            break
        }
    }

    private func connectIfReady() {
        guard wanted, let id = accessory?.bluetoothIdentifier else {
            if accessory == nil { state = .notSetUp }
            return
        }
        // The central only powers on once the app has an accessory, so it is made here, not earlier.
        if central == nil {
            central = CBCentralManager(delegate: self, queue: .main,
                                       options: [CBCentralManagerOptionRestoreIdentifierKey: "slot17.bluetooth"])
            return                                      // centralManagerDidUpdateState continues
        }
        guard let central, central.state == .poweredOn else { return }
        if peripheral == nil { peripheral = central.retrievePeripherals(withIdentifiers: [id]).first }
        guard let peripheral else { return }
        peripheral.delegate = self
        if peripheral.state == .disconnected {
            state = .connecting
            central.connect(peripheral)                 // never times out: completes when the car computer is in range
        }
    }

    // MARK: CBCentralManagerDelegate

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn {
            connectIfReady()
        } else if central.state == .poweredOff {
            state = .bluetoothOff
        }
    }

    func centralManager(_ central: CBCentralManager, willRestoreState dict: [String: Any]) {
        if let restored = (dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral])?.first {
            peripheral = restored
            restored.delegate = self
        }
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        mtu = peripheral.maximumWriteValueLength(for: .withoutResponse) + 3
        peripheral.discoverServices([Self.service])
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        mtu = nil
        guard wanted, state != .wrongCar else { return }
        state = .connecting
        central.connect(peripheral)
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        guard wanted, state != .wrongCar else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.retryAfter) { [weak self] in
            guard let self, self.wanted, self.state != .wrongCar else { return }
            central.connect(peripheral)
        }
    }

    // MARK: CBPeripheralDelegate

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard let service = peripheral.services?.first(where: { $0.uuid == Self.service }) else {
            // Also while the car computer's own Bluetooth link starts again: its service is back moments later.
            retry(peripheral, error.map { "services: \($0.localizedDescription)" } ?? "the car computer has no Slot17 service yet")
            return
        }
        peripheral.discoverCharacteristics([Self.frames, Self.info, Self.control], for: service)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        controlCharacteristic = service.characteristics?.first { $0.uuid == Self.control }
        guard let info = service.characteristics?.first(where: { $0.uuid == Self.info }) else {
            retry(peripheral, error.map { "characteristics: \($0.localizedDescription)" } ?? "the Slot17 service is incomplete")
            return
        }
        peripheral.readValue(for: info)
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            if characteristic.uuid == Self.info { retry(peripheral, "reading the car computer: \(error.localizedDescription)") }
            return
        }
        guard let value = characteristic.value else { return }
        if characteristic.uuid == Self.frames {
            onFrame(value)
        } else if characteristic.uuid == Self.info {
            let info = (try? JSONSerialization.jsonObject(with: value)) as? [String: Any]
            let keyID = info?["key_id"] as? String
            if let expectedKeyID, keyID != expectedKeyID {
                note("PROBLEM Bluetooth: this is a different car computer (key \(keyID ?? "?")); set it up again")
                state = .wrongCar
                central?.cancelPeripheralConnection(peripheral)
                return
            }
            if let frames = characteristic.service?.characteristics?.first(where: { $0.uuid == Self.frames }) {
                peripheral.setNotifyValue(true, for: frames)
            }
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error { note("PROBLEM Bluetooth: the car computer refused a command: \(error.localizedDescription)") }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        guard characteristic.uuid == Self.frames else { return }
        if characteristic.isNotifying {
            state = .connected
            note("Bluetooth connected to the car computer (MTU \(mtu.map(String.init) ?? "default"))")
        } else if let error {
            retry(peripheral, "no notifications: \(error.localizedDescription)")
        }
    }
}
