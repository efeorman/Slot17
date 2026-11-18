import Foundation
import Security

private let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]

/// The diagnostics log. Written line by line, so a killed app still leaves its last words.
final class EventLog {
    private static let rotateBytes = 2_000_000
    let url = documents.appendingPathComponent("slot17.log")
    private let older = documents.appendingPathComponent("slot17.older.log")
    private let stamp: ISO8601DateFormatter
    private(set) var recent: [String] = []
    /// The file is written here, in order, and never on the main thread: a line comes with every
    /// match, and on a nearly full phone one write can take long enough to make the app stutter.
    private let queue = DispatchQueue(label: "nl.slot17.log", qos: .utility)
    /// Kept open between lines. Used only on `queue`.
    private var handle: FileHandle?

    init() {
        stamp = ISO8601DateFormatter()
        stamp.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        prepare()
    }

    private func prepare() {
        if !FileManager.default.fileExists(atPath: url.path) {
            // Must stay writable while the phone is locked, which is how this app is used.
            FileManager.default.createFile(atPath: url.path, contents: nil, attributes: [
                .protectionKey: FileProtectionType.completeUntilFirstUserAuthentication,
            ])
        }
    }

    func write(_ text: String) {
        let line = "\(stamp.string(from: Date())) \(text)"
        recent.append(line)
        if recent.count > 300 { recent.removeFirst(recent.count - 300) }
        guard let data = (line + "\n").data(using: .utf8) else { return }
        queue.async { self.append(data) }
    }

    private func append(_ data: Data) {
        if handle == nil {
            prepare()
            handle = try? FileHandle(forWritingTo: url)
            _ = try? handle?.seekToEnd()
        }
        guard let file = handle else { return }
        do {
            try file.write(contentsOf: data)
        } catch {
            try? file.close()
            handle = nil
            return
        }
        let end = (try? file.offset()) ?? 0
        if end > UInt64(Self.rotateBytes) {
            try? file.close()
            handle = nil
            try? FileManager.default.removeItem(at: older)
            try? FileManager.default.moveItem(at: url, to: older)
            prepare()
        }
    }

    /// Waits until every line so far is in the file, for when the app is about to stop.
    func flush() {
        queue.sync {}
    }

    /// The newest part of the log, within what the server accepts. Read off the main thread; the
    /// answer comes back on it.
    func tail(maxBytes: Int, done: @escaping (Data) -> Void) {
        queue.async {
            var data = Data()
            for path in [self.older, self.url] { if let part = try? Data(contentsOf: path) { data.append(part) } }
            let newest = data.count > maxBytes ? Data(data.suffix(maxBytes)) : data
            DispatchQueue.main.async { done(newest) }
        }
    }

    func clear() {
        queue.sync {
            try? handle?.close()
            handle = nil
            try? FileManager.default.removeItem(at: url)
            try? FileManager.default.removeItem(at: older)
            prepare()
        }
        recent.removeAll()
    }
}

/// Small JSON files for sightings and alert history.
enum JSONStore {
    static func load<T: Decodable>(_ name: String, as type: T.Type) -> T? {
        guard let data = try? Data(contentsOf: documents.appendingPathComponent(name)) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    /// One queue, so saves of the same file land in order. Encoding and writing happen off the main
    /// thread: with a thousand alerts, a save on a tap would otherwise make the button stutter.
    private static let queue = DispatchQueue(label: "nl.slot17.save", qos: .utility)

    static func save<T: Encodable>(_ value: T, _ name: String) {
        let url = documents.appendingPathComponent(name)
        queue.async {
            guard let data = try? JSONEncoder().encode(value) else { return }
            try? data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        }
    }

    /// Written before returning, with an answer: for data with no other copy once this succeeds.
    static func saveNow<T: Encodable>(_ value: T, _ name: String) -> Bool {
        let url = documents.appendingPathComponent(name)
        return queue.sync {
            guard let data = try? JSONEncoder().encode(value) else { return false }
            return (try? data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])) != nil
        }
    }

    /// Waits for every save so far, for when the app is about to be suspended.
    static func flush() {
        queue.sync {}
    }
}

/// The private link is a secret, so it lives in the Keychain and never in the app's files or
/// settings. Readable after the first unlock, because the app runs with the phone locked.
enum Keychain {
    private static let account = "slot17-private-link"
    private static var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account]
    }

    static func save(_ link: String) -> Bool {
        let values: [String: Any] = [kSecValueData as String: Data(link.utf8),
                                     kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock]
        let updated = SecItemUpdate(query as CFDictionary, values as CFDictionary)
        if updated != errSecItemNotFound { return updated == errSecSuccess }
        return SecItemAdd(query.merging(values) { $1 } as CFDictionary, nil) == errSecSuccess
    }

    static func load() -> String? {
        var request = query
        request[kSecReturnData as String] = true
        var out: AnyObject?
        guard SecItemCopyMatching(request as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func delete() { SecItemDelete(query as CFDictionary) }
}
