import Foundation

/// Support packages from the app (1.5.0): the car computer makes one (p0/support.py) and uploads it to the server;
/// here it is listed, shared through the share sheet, sent to the Slot17 team with the person's consent, taken back
/// from them, or deleted. `phoneFiles` uploads this phone's log and, only when asked, its drive data with places
/// before the car computer is asked, so the server can add them to the package.
final class SupportRoute: ObservableObject {
    @Published private(set) var packages: [SupportPackage] = []
    /// Asking the car computer for a package.
    @Published private(set) var request = ActionState()
    /// Sharing, sending, taking back or deleting one.
    @Published private(set) var fileAction = ActionState()
    /// A downloaded package for the share sheet; the card shows it and clears this.
    @Published var share: URL?

    var client: ServerClient?
    var sendOp: (String, [String: Any], @escaping (ActionState) -> Void) -> Void = { _, _, _ in }
    var carState: () -> PiState? = { nil }
    var phoneFiles: (Bool, Bool, @escaping () -> Void) -> Void = { _, _, then in then() }
    var note: (String) -> Void = { _ in }

    var drives: [DriveRow] { carState()?.drives ?? [] }
    var progress: String? { SupportWords.progress(carState()?.support) }

    var unavailable: String? {
        guard let state = carState() else { return "The car computer has not reported yet" }
        if !(state.features ?? []).contains("support") { return "The car computer needs software 1.5.0 for this" }
        return nil
    }

    func create(choice: String, session: String?, window: [Int]?, recordings: Bool, phoneLog: Bool, places: Bool) {
        guard unavailable == nil, !request.busy else { return }
        var fields: [String: Any] = ["choice": choice, "phone_log": phoneLog, "places": places]
        if choice != "quick", let session { fields["session"] = session }
        if choice != "quick", let window { fields["window"] = window }
        if choice == "everything" { fields["recordings"] = recordings }
        note("support package requested: \(SupportWords.choice(choice))\(places ? ", with places" : "")")
        request = .init(text: "Preparing…", busy: true)
        phoneFiles(phoneLog, places) { [weak self] in
            self?.sendOp("support", fields) { self?.request = $0 }
        }
    }

    /// The server's answer for the request: the car computer's ack, with the package's name when it is ready.
    func apply(_ op: OpStatus) {
        guard op.id == request.id else { return }
        switch op.state {
        case "done":
            request = .init(id: op.id, text: "Ready: \(op.detail)", level: .ok)
            refresh()
        case "failed", "expired":
            request = .init(id: op.id, text: op.detail.isEmpty ? "It did not work" : op.detail, level: .bad)
        default:
            request = .init(id: op.id, text: progress ?? "Waiting for the car computer…", level: .idle, busy: true)
        }
    }

    func refresh() {
        client?.listSupport { [weak self] result in
            switch result {
            case .success(let rows): self?.packages = rows
            case .failure(let error): self?.fileAction = .init(text: "Could not list them: \(error.localizedDescription)", level: .bad)
            }
        }
    }

    func shareFile(_ p: SupportPackage) {
        fileAction = .init(text: "Downloading \(SupportWords.size(p.size))…", busy: true)
        client?.downloadSupport(p.name) { [weak self] result in
            switch result {
            case .success(let url):
                self?.fileAction = ActionState()
                self?.share = url
            case .failure(let error):
                self?.fileAction = .init(text: "Could not download it: \(error.localizedDescription)", level: .bad)
            }
        }
    }

    func sendToTeam(_ p: SupportPackage) {
        action("send", p, busy: "Sending to the Slot17 team…", consent: true) { [weak self] body in
            let code = body["code"] as? String ?? "?"
            self?.note("support package sent to the Slot17 team as \(code)")
            return "Sent. Your code is \(code): give it to the Slot17 team. They delete it after 30 days."
        }
    }

    func takeBack(_ p: SupportPackage) {
        action("unsend", p, busy: "Taking it back…") { _ in "Deleted from the Slot17 team's inbox" }
    }

    func delete(_ p: SupportPackage) {
        action("delete", p, busy: "Deleting…") { body in
            switch body["team"] as? String {
            case "deleted": return "Deleted from your server and from the Slot17 team's inbox"
            case "left": return "Deleted from your server. The Slot17 team's inbox did not answer; their copy goes after its 30 days."
            default: return "Deleted from your server"
            }
        }
    }

    private func action(_ kind: String, _ p: SupportPackage, busy: String, consent: Bool = false,
                        done: @escaping ([String: Any]) -> String) {
        fileAction = .init(text: busy, busy: true)
        var json: [String: Any] = ["name": p.name]
        if consent { json["consent"] = true }
        client?.supportAction(kind, json: json) { [weak self] result in
            switch result {
            case .success(let data):
                let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
                self?.fileAction = .init(text: done(body), level: .ok)
                self?.refresh()
            case .failure(let error):
                let why: String
                switch ServerClient.status(of: error) {
                case 400: why = "the server refused it (a package to send must be under 50 MB)"
                case 502: why = "the Slot17 team's inbox did not answer; try again later"
                default: why = error.localizedDescription
                }
                self?.fileAction = .init(text: "It did not work: \(why)", level: .bad)
            }
        }
    }
}
