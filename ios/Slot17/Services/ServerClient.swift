import Foundation

/// All network traffic to the display server. Everything arrives on the main queue.
///
/// The operator has chosen plain HTTP to this server, so the key and all of this traffic travel
/// unencrypted; that includes power off and stop.
final class ServerClient: NSObject, URLSessionDataDelegate {
    enum HTTPError: LocalizedError {
        case status(Int), noData, noLink
        /// No link: until 27 November 2026 such a request never answered, and its button said "Sending…" for good.
        var errorDescription: String? {
            if case .noLink = self { return "no server is set up" }
            return nil
        }
    }

    static func status(of error: Error) -> Int? {
        if case .status(let code)? = error as? HTTPError { return code }
        return nil
    }

    var onMessage: (ServerMessage) -> Void = { _ in }
    var onStreamEvent: (String) -> Void = { _ in }
    var onUnauthorised: () -> Void = {}
    private(set) var link: ServerLink?
    private(set) var streamOpens = 0
    private(set) var openedAt = 0.0

    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 12
        config.timeoutIntervalForResource = 7 * 24 * 3600
        config.waitsForConnectivity = false
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }()
    private var stream: URLSessionDataTask?
    private var parser = SSEParser()
    private let decoder = JSONDecoder()

    func configure(_ link: ServerLink?) {
        self.link = link
        closeStream()
    }

    private func request(_ path: String, method: String = "GET", timeout: TimeInterval? = nil) -> URLRequest? {
        guard let link else { return nil }
        var request = URLRequest(url: link.base.appendingPathComponent(path))
        request.httpMethod = method
        request.setValue("s17=\(link.key)", forHTTPHeaderField: "Cookie")
        request.setValue("Slot17-iOS", forHTTPHeaderField: "User-Agent")
        if let timeout { request.timeoutInterval = timeout }
        return request
    }

    // MARK: Live stream

    func openStream() {
        closeStream()
        guard let request = request("api/stream") else { return }
        parser = SSEParser()
        openedAt = Mono.now()
        streamOpens += 1
        let task = session.dataTask(with: request)
        stream = task
        task.resume()
    }

    func closeStream() {
        stream?.cancel()
        stream = nil
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard dataTask === stream else { return completionHandler(.cancel) }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 { onUnauthorised() }
        if status != 200 { onStreamEvent("stream answered \(status)") }
        completionHandler(status == 200 ? .allow : .cancel)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard dataTask === stream else { return }
        for text in parser.feed(data) {
            if let message = try? decoder.decode(ServerMessage.self, from: Data(text.utf8)) {
                onMessage(message)
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard task === stream else { return }
        stream = nil
        if let error = error as NSError?, error.code != NSURLErrorCancelled {
            onStreamEvent("stream ended: \(error.localizedDescription)")
        }
    }

    // MARK: Requests

    /// A timed request. Its duration bounds the clock offset, so it is measured here.
    func probe(_ done: @escaping (Result<ServerMessage, Error>, _ begin: Double, _ end: Double) -> Void) {
        guard var request = request("api/state") else { return }
        request.timeoutInterval = 4
        let begin = Mono.now()
        session.dataTask(with: request) { [decoder] data, response, error in
            let end = Mono.now()
            let result: Result<ServerMessage, Error>
            if let error {
                result = .failure(error)
            } else if let status = (response as? HTTPURLResponse)?.statusCode, status != 200 {
                result = .failure(HTTPError.status(status))
            } else if let data, let message = try? decoder.decode(ServerMessage.self, from: data) {
                result = .success(message)
            } else {
                result = .failure(HTTPError.noData)
            }
            DispatchQueue.main.async { done(result, begin, end) }
        }.resume()
    }

    func send(path: String, json: [String: Any], done: @escaping (Result<Data, Error>) -> Void) {
        guard var request = request(path, method: "POST", timeout: 8),
              let body = try? JSONSerialization.data(withJSONObject: json) else { return noLink(done) }
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        perform(request, done)
    }

    func uploadLog(_ body: Data, done: @escaping (Result<Data, Error>) -> Void) {
        guard var request = request("api/app-log", method: "POST", timeout: 60) else { return noLink(done) }
        request.httpBody = body
        request.setValue("text/plain; charset=utf-8", forHTTPHeaderField: "Content-Type")
        perform(request, done)
    }

    func listLogs(done: @escaping (Result<[ServerLogEntry], Error>) -> Void) {
        guard let request = request("api/logs", timeout: 10) else { return noLink(done) }
        perform(request) { [decoder] result in
            done(result.flatMap { data -> Result<[ServerLogEntry], Error> in
                Result(catching: { try decoder.decode(Listing.self, from: data).logs })
            })
        }
    }

    func fetchLog(_ name: String, done: @escaping (Result<String, Error>) -> Void) {
        guard let request = request("api/logs/\(name)", timeout: 30) else { return noLink(done) }
        perform(request) { result in
            done(result.map { String(decoding: $0, as: UTF8.self) })
        }
    }

    /// The app's settings file from the server. 404 means the server has none: keep the defaults.
    func fetchConfig(done: @escaping (Result<Data, Error>) -> Void) {
        guard let request = request("api/config", timeout: 10) else { return noLink(done) }
        perform(request, done)
    }

    func fetchSound(_ name: String, done: @escaping (Result<Data, Error>) -> Void) {
        guard let request = request("api/sounds/\(name)", timeout: 20) else { return noLink(done) }
        perform(request, done)
    }

    private struct Listing: Decodable { let logs: [ServerLogEntry] }

    private func noLink<T>(_ done: @escaping (Result<T, Error>) -> Void) {
        DispatchQueue.main.async { done(.failure(HTTPError.noLink)) }
    }

    // MARK: Support packages (1.5.0)

    func listSupport(done: @escaping (Result<[SupportPackage], Error>) -> Void) {
        guard let request = request("api/support", timeout: 10) else { return noLink(done) }
        perform(request) { [decoder] result in
            done(result.flatMap { data -> Result<[SupportPackage], Error> in
                Result(catching: { try decoder.decode(SupportListing.self, from: data).packages })
            })
        }
    }

    /// A package to a file in Caches/support, for the share sheet: it can be 100 MB, so it is not held in memory.
    func downloadSupport(_ name: String, done: @escaping (Result<URL, Error>) -> Void) {
        guard let request = request("api/support/\(name)", timeout: 120) else { return done(.failure(HTTPError.noData)) }
        session.downloadTask(with: request) { temp, response, error in
            let result: Result<URL, Error>
            if let error {
                result = .failure(error)
            } else if let status = (response as? HTTPURLResponse)?.statusCode, status != 200 {
                result = .failure(HTTPError.status(status))
            } else if let temp {
                result = Result {
                    let folder = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
                        .appendingPathComponent("support", isDirectory: true)
                    try? FileManager.default.removeItem(at: folder)          // only the newest one is kept
                    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                    let dest = folder.appendingPathComponent(name)
                    try FileManager.default.moveItem(at: temp, to: dest)
                    return dest
                }
            } else {
                result = .failure(HTTPError.noData)
            }
            DispatchQueue.main.async { done(result) }
        }.resume()
    }

    /// Delete, send to the Slot17 team or take back: sending can take a minute, so a longer wait.
    func supportAction(_ action: String, json: [String: Any], done: @escaping (Result<Data, Error>) -> Void) {
        guard var request = request("api/support/\(action)", method: "POST", timeout: 150),
              let body = try? JSONSerialization.data(withJSONObject: json) else { return done(.failure(HTTPError.noData)) }
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        perform(request, done)
    }

    private func perform(_ request: URLRequest, _ done: @escaping (Result<Data, Error>) -> Void) {
        session.dataTask(with: request) { data, response, error in
            let result: Result<Data, Error>
            if let error {
                result = .failure(error)
            } else if let status = (response as? HTTPURLResponse)?.statusCode, status != 200 {
                result = .failure(HTTPError.status(status))
            } else {
                result = .success(data ?? Data())
            }
            DispatchQueue.main.async { done(result) }
        }.resume()
    }
}
