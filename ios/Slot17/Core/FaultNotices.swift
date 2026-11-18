import Foundation

/// The car computer's faults as notifications from this app, the same faults Pushover reports.
///
/// One notice when a fault starts and one when everything is fine again, never a reminder per minute,
/// and at most one notice a minute (the Pi's own Pushover rule). A fault seen during the wait is kept
/// and sent after it. No fresh car data is not a fault here: parked, that is normal, and in the car the
/// link-lost tone already says it.
///
/// This works only while iPhone keeps the app running, as it does with sound armed. A closed or
/// sleeping app notices nothing; Pushover from the car does not have that limit.
struct FaultNotices {
    struct Input: Equatable {
        /// The detector's state from fresh car data, or nil when there is none.
        var detectorState: String?
        var reasons: [String] = []
        var receiverQuiet = false
        /// The operator stopped it or powered the Pi off from the app.
        var expectingSilence = false
    }

    struct Notice: Equatable {
        var title: String
        var body: String
    }

    static let minGap = 60.0

    /// The faults already announced in this episode.
    private(set) var announced: Set<String> = []
    private var lastSentAt = -Double.infinity

    static func faults(_ input: Input) -> [String: String] {
        var out: [String: String] = [:]
        switch input.detectorState {
        case "degraded"?:
            let reasons = input.reasons.filter { !$0.isEmpty }
            if reasons.isEmpty { out["degraded"] = "Detector degraded" }
            for reason in reasons { out["degraded: \(reason)"] = reason }
        case "stopped"?:
            if !input.expectingSilence { out["stopped"] = "Detector stopped" }
        default:
            break
        }
        if input.receiverQuiet { out["quiet"] = "The receiver hears nothing" }
        return out
    }

    mutating func step(_ input: Input, now: Double) -> Notice? {
        guard input.detectorState != nil, now - lastSentAt >= Self.minGap else { return nil }
        let faults = Self.faults(input)
        let new = faults.filter { !announced.contains($0.key) }
        if !new.isEmpty {
            announced.formUnion(new.keys)
            lastSentAt = now
            let words = new.keys.sorted().compactMap { new[$0] }
            return Notice(title: "Slot17: \(words.first ?? "fault")",
                          body: words.count > 1 ? words.joined(separator: ", ") : Self.advice(new.keys.sorted().first ?? ""))
        }
        // Fine again only when running with nothing wrong; "starting" waits.
        if faults.isEmpty, !announced.isEmpty, input.detectorState == "running" {
            announced.removeAll()
            lastSentAt = now
            return Notice(title: "Slot17: running normally again", body: "The fault has cleared.")
        }
        // A fault that cleared while another remains is forgotten, so it is announced if it returns. Only
        // in states that show every fault: "starting" shows none yet, and must not end the episode.
        if input.detectorState == "running" || input.detectorState == "degraded" {
            announced.formIntersection(faults.keys)
        }
        return nil
    }

    private static func advice(_ key: String) -> String {
        switch key {
        case "quiet": return "Check the antenna cable. Input level is not calibrated, so this is a hint, not a proof."
        case "stopped": return "It starts again only when the Pi next powers up."
        default: return "See the Pi screen in the app for details."
        }
    }
}
