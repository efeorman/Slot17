import Foundation

/// The newest published Slot17 release, as the display server read it from GitHub (server 1.2.9 and later).
/// `app` is missing for releases without a versions.json.
struct ReleaseInfo: Decodable, Equatable {
    var software: String
    var app: String?
    var tag: String?
    var url: String?
}

/// Which updates to mention. Versions compare by their numbers, so 1.10.0 is newer than 1.9.0.
enum Updates {
    static func numbers(_ version: String) -> [Int] {
        version.split(separator: ".").map { part in Int(part.prefix(while: { $0.isNumber })) ?? 0 }
    }

    static func isNewer(_ a: String, than b: String) -> Bool {
        let x = numbers(a), y = numbers(b)
        for i in 0..<max(x.count, y.count) {
            let p = i < x.count ? x[i] : 0, q = i < y.count ? y[i] : 0
            if p != q { return p > q }
        }
        return false
    }

    /// The newer version, or nil when there is none or either side is unknown.
    static func newer(_ published: String?, than running: String?) -> String? {
        guard let published, let running, !running.isEmpty, isNewer(published, than: running) else { return nil }
        return published
    }

    /// One line for a notification, or nil. The same text is never announced twice.
    static func announcement(app: String?, software: String?) -> String? {
        var parts: [String] = []
        if let app { parts.append("app \(app)") }
        if let software { parts.append("car computer software \(software)") }
        return parts.isEmpty ? nil : parts.joined(separator: " and ")
    }
}
