import Foundation
import MetricKit

/// Apple's own daily measurements (CPU, GPS time, network, background time) and crash and hang reports,
/// put in the app's log so they arrive with it. iOS decides when these come, typically once a day.
final class PhoneReports: NSObject, MXMetricManagerSubscriber {
    private static let limit = 60_000
    private let note: (String) -> Void

    init(note: @escaping (String) -> Void) {
        self.note = note
        super.init()
        MXMetricManager.shared.add(self)
    }

    deinit { MXMetricManager.shared.remove(self) }

    func didReceive(_ payloads: [MXMetricPayload]) {
        for payload in payloads { write("daily measurements", payload.jsonRepresentation()) }
    }

    func didReceive(_ payloads: [MXDiagnosticPayload]) {
        for payload in payloads { write("crash or hang report", payload.jsonRepresentation()) }
    }

    private func write(_ title: String, _ json: Data) {
        let text = String(decoding: json.prefix(Self.limit), as: UTF8.self).replacingOccurrences(of: "\n", with: " ")
        let cut = json.count > Self.limit ? " (cut at \(Self.limit) bytes of \(json.count))" : ""
        DispatchQueue.main.async { self.note("\(title) from iOS\(cut): \(text)") }
    }
}
