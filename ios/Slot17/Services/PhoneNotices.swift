import UserNotifications

/// Posts the car computer's faults as ordinary iPhone notifications (see `FaultNotices` for when).
/// Local notifications from this app: no server, no Pushover account, nothing leaves the phone.
final class PhoneNotices: NSObject, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()
    private let log: (String) -> Void
    /// The last answer iOS gave, in words, for the Settings screen.
    private(set) var permission = "Not asked yet"
    var onChange: (() -> Void)?

    init(log: @escaping (String) -> Void) {
        self.log = log
        super.init()
        center.delegate = self
        readPermission()
    }

    /// Asks once; iOS remembers the answer and shows no dialog after that.
    func ask() {
        center.requestAuthorization(options: [.alert, .sound]) { [weak self] granted, error in
            let why = error.map { ": " + $0.localizedDescription } ?? ""
            DispatchQueue.main.async {
                self?.log(granted ? "notifications allowed" : "notifications not allowed" + why)
                self?.readPermission()
            }
        }
    }

    func readPermission() {
        center.getNotificationSettings { [weak self] settings in
            let text: String
            switch settings.authorizationStatus {
            case .authorized, .provisional, .ephemeral: text = "Allowed"
            case .denied: text = "Not allowed: switch it on in iPhone Settings, Notifications, Slot17"
            default: text = "Not asked yet"
            }
            DispatchQueue.main.async {
                guard let self, self.permission != text else { return }
                self.permission = text
                self.onChange?()
            }
        }
    }

    func post(_ notice: FaultNotices.Notice) {
        let content = UNMutableNotificationContent()
        content.title = notice.title
        content.body = notice.body
        content.sound = .default
        content.threadIdentifier = "slot17-faults"
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        center.add(request) { [weak self] error in
            if let error { DispatchQueue.main.async { self?.log("PROBLEM notification not posted: \(error.localizedDescription)") } }
        }
        log("notification: \(notice.title)")
    }

    /// Shown as a banner while the app is open too; the app's own beeps are separate and unchanged.
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list])
    }
}
