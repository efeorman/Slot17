import AVFoundation
import UIKit

/// Keeps the app running and plays alert sounds, with the screen locked and over other apps.
///
/// A near-silent mixable loop is what keeps iOS running the app. The sound parts here came from
/// the sound test app and the faults its simulator tests found: both players are reset on every
/// engine start, and a sound counts as played only if the engine kept running, its whole time
/// passed and real signal left the mixer. Nothing here can tell whether the car's speakers made it.
final class AudioAlerts {
    enum Result {
        case played(SoundKind, peakDB: Double)
        case failed(SoundKind, String)
    }

    var log: (String) -> Void = { _ in }
    var onResult: (Result) -> Void = { _ in }
    var onStateChange: () -> Void = {}
    var volume: Float = 1.0
    var style: BeepStyle = .mix

    private(set) var armed = false
    private(set) var interrupted = false
    private(set) var soundsPlayed = 0
    private(set) var recoveries = 0
    private(set) var lastPeakDB: Double?

    private let session = AVAudioSession.sharedInstance()
    private let format = AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 1)!
    private var engine = AVAudioEngine()
    private var keepAlive = AVAudioPlayerNode()
    private var player = AVAudioPlayerNode()
    private lazy var quietLoop = Self.makeQuietLoop(format)
    private lazy var sounds: [SoundKind: AVAudioPCMBuffer] = Dictionary(uniqueKeysWithValues:
        [SoundKind.urgent, .chime, .call, .warning, .linkLost, .marked].map { ($0, Self.buffer(SoundSynth.render($0.builtIn), format)) })
    private var watchdog: DispatchSourceTimer?
    private var switching = false
    private var retry: DispatchWorkItem?
    private var interruptedAt: Date?
    private var lastStuckAttempt = Date.distantPast
    private var styleToken = 0
    private var rebuildAfterSwitch = false
    private var generation = 0
    /// An urgent beep that arrived during a chime in a mode that re-activates the session. Plays right after it.
    private var urgentWaiting = false
    private let peakLock = NSLock()
    private var outputPeak: Float = 0
    /// Sounds scheduled and not finished. They play one after another, so this limits any backlog.
    private var soundsUnderway = 0
    private static let maxSoundsUnderway = 2

    #if DEBUG
    /// Test builds on a Mac where sound hangs. The simulator-wide setting cannot be missed by a launch.
    static var skippedForTests: Bool {
        ProcessInfo.processInfo.arguments.contains("--no-audio") || ProcessInfo.processInfo.environment["SLOT17_NO_AUDIO"] == "1"
    }
    #endif

    init() {
        let center = NotificationCenter.default
        center.addObserver(self, selector: #selector(interruption(_:)),
                           name: AVAudioSession.interruptionNotification, object: session)
        center.addObserver(self, selector: #selector(routeChanged(_:)),
                           name: AVAudioSession.routeChangeNotification, object: session)
        center.addObserver(self, selector: #selector(mediaReset),
                           name: AVAudioSession.mediaServicesWereResetNotification, object: session)
        center.addObserver(self, selector: #selector(engineChanged(_:)),
                           name: .AVAudioEngineConfigurationChange, object: nil)
        #if DEBUG
        // Connecting to the mixer already wakes CoreAudio, which is what deadlocks on an audio-less Mac.
        if Self.skippedForTests { return }
        #endif
        attachNodes()
    }

    // MARK: State for the screen

    var routeName: String {
        session.currentRoute.outputs.map { output in
            output.portType == .carAudio ? "CarPlay" : output.portName
        }.joined(separator: ", ")
    }

    var routeIsCar: Bool { session.currentRoute.outputs.contains { $0.portType == .carAudio } }
    var otherAudioPlaying: Bool { session.isOtherAudioPlaying }
    var engineRunning: Bool { engine.isRunning }
    /// Asked for, but not running at the moment. Separate from `problem`, so alert decisions do not change.
    var recovering: Bool {
        #if DEBUG
        if Self.skippedForTests { return false }
        #endif
        return armed && !interrupted && !switching && !engine.isRunning
    }

    /// Why a sound could not play right now, in words, or nil if it can.
    var problem: String? {
        if !armed { return "sound is not armed" }
        if interrupted { return "a call or Siri is using the audio" }
        return nil
    }

    // MARK: Control

    /// Must first be called with the app open: iOS will not start this from the background.
    func arm() {
        guard !armed else { return }
        #if DEBUG
        // Test builds only: a Mac with no audio system deadlocks CoreAudio and iOS kills the app.
        if Self.skippedForTests {
            armed = true
            log("audio skipped for this test run")
            onStateChange()
            return
        }
        #endif
        do {
            try holdQuietSession()
            try startEngine()
            armed = true
            interrupted = false
            startWatchdog()
            log("audio armed, output \(routeName)")
        } catch {
            // Armed all the same, so sounds report the real reason, and it keeps trying: a call during
            // the open must not leave a whole drive silent with the phone locked.
            log("PROBLEM audio could not start: \(error.localizedDescription). Trying again")
            armed = true
            let next = DispatchWorkItem { [weak self] in self?.recover("the first start failed") }
            retry = next
            DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: next)
        }
        onStateChange()
    }

    func disarm(_ why: String) {
        guard armed else { return }
        armed = false
        urgentWaiting = false
        styleToken += 1
        switching = false
        interrupted = false
        interruptedAt = nil
        rebuildAfterSwitch = false
        watchdog?.cancel(); watchdog = nil
        retry?.cancel()
        engine.stop()
        try? session.setActive(false, options: .notifyOthersOnDeactivation)
        log("audio disarmed: \(why)")
        onStateChange()
    }

    /// Returns nil if the sound was started, or the reason it was not. Never queues.
    @discardableResult
    func play(_ kind: SoundKind) -> String? {
        guard armed else { return "sound is not armed" }
        #if DEBUG
        // Reports a pretend result, so everything after the sound can be tested. scripts/check_e2e.py
        // does not count these as measured sound.
        if Self.skippedForTests {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
                self?.soundsPlayed += 1
                self?.onResult(.played(kind, peakDB: -120))
            }
            return nil
        }
        #endif
        if switching {
            if kind == .urgent {
                urgentWaiting = true
                return nil
            }
            return "the previous sound is still playing"
        }
        if style == .mix || kind == .linkLost || kind == .marked {
            return start(kind, then: nil)
        }
        // Ducking begins when a session activates and must last only seconds (Apple), so the
        // session is re-activated around the sound and the long-held one stays plain mixable.
        switching = true
        do {
            engine.stop()
            try session.setActive(false)
            if style == .duck {
                try session.setCategory(.playback, mode: .default, options: [.mixWithOthers, .duckOthers])
            } else {
                try session.setCategory(.playback, mode: .voicePrompt,
                                        options: [.interruptSpokenAudioAndMixWithOthers, .duckOthers])
            }
            try session.setActive(true)
            try startEngine()
        } catch {
            switching = false
            log("PROBLEM \(style.rawValue) refused: \(error.localizedDescription). Playing over the music instead")
            recover("after refused \(style.rawValue)")
            return start(kind, then: nil)
        }
        styleToken += 1
        let token = styleToken
        let finish: () -> Void = { [weak self] in
            guard let self, self.styleToken == token else { return }
            self.styleToken += 1
            self.restoreAfterStyle()
            if self.urgentWaiting {
                self.urgentWaiting = false
                if let refused = self.play(.urgent) { self.log("PROBLEM urgent not played after the chime: \(refused)") }
            }
        }
        // Give the music a moment to dip, or the first pulse lands on full-volume music. This was 0.4 s
        // for every sound; on the 12 November drive that was a fifth of the whole delay from burst to
        // beep. The urgent beep is loud enough to start almost at once.
        let leadIn = kind == .urgent ? 0.05 : 0.15
        DispatchQueue.main.asyncAfter(deadline: .now() + leadIn) { [weak self] in
            guard let self, self.styleToken == token else { return }
            if let refused = self.start(kind, then: finish) {
                self.log("PROBLEM \(kind.rawValue) not played: \(refused)")
                self.onResult(.failed(kind, refused))
                finish()
            }
        }
        // A sound that never reports finishing must not leave the music down for the drive. The wait
        // counts from the moment it starts, so a long sound from the server is not cut off.
        let length = sounds[kind].map { Double($0.frameLength) / format.sampleRate } ?? 0
        DispatchQueue.main.asyncAfter(deadline: .now() + max(4, leadIn + length + 1)) { [weak self] in
            guard let self, self.styleToken == token else { return }
            self.log("PROBLEM \(kind.rawValue) never finished: restoring the music")
            finish()
        }
        return nil
    }

    // MARK: Session and engine

    private func holdQuietSession() throws {
        try session.setCategory(.playback, mode: .default, options: [.mixWithOthers])
        try session.setActive(true)
    }

    private func attachNodes() {
        engine.attach(keepAlive)
        engine.attach(player)
        engine.connect(keepAlive, to: engine.mainMixerNode, format: format)
        engine.connect(player, to: engine.mainMixerNode, format: format)
    }

    private func startEngine() throws {
        engine.prepare()
        try engine.start()
        generation += 1
        soundsUnderway = 0
        watchLevel()
        keepAlive.stop()
        player.stop()
        keepAlive.scheduleBuffer(quietLoop, at: nil, options: .loops)
        keepAlive.play()
        player.play()
    }

    private func watchLevel() {
        let mixer = engine.mainMixerNode
        mixer.removeTap(onBus: 0)
        mixer.installTap(onBus: 0, bufferSize: 2048, format: nil) { [weak self] buffer, _ in
            guard let self, let channels = buffer.floatChannelData else { return }
            var peak: Float = 0
            for channel in 0..<Int(buffer.format.channelCount) {
                for i in 0..<Int(buffer.frameLength) { peak = max(peak, abs(channels[channel][i])) }
            }
            self.peakLock.lock()
            self.outputPeak = max(self.outputPeak, peak)
            self.peakLock.unlock()
        }
    }

    private func takePeak() -> Float {
        peakLock.lock()
        defer { peakLock.unlock() }
        let peak = outputPeak
        outputPeak = 0
        return peak
    }

    private func currentPeak() -> Float {
        peakLock.lock()
        defer { peakLock.unlock() }
        return outputPeak
    }

    private func recover(_ why: String, attempt: Int = 1) {
        guard armed else { return }
        retry?.cancel()
        do {
            try holdQuietSession()
            try startEngine()
            interrupted = false
            interruptedAt = nil
            recoveries += 1
            log("audio recovered (\(why), attempt \(attempt))")
            if watchdog == nil { startWatchdog() }
            onStateChange()
        } catch {
            if attempt <= 3 || attempt % 15 == 0 {
                log("audio recovery refused (\(why), attempt \(attempt)): \(error.localizedDescription)")
            }
            let next = DispatchWorkItem { [weak self] in self?.recover(why, attempt: attempt + 1) }
            retry = next
            DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: next)
        }
    }

    private func rebuild(_ why: String) {
        guard armed else { return }
        engine.stop()
        engine = AVAudioEngine()
        keepAlive = AVAudioPlayerNode()
        player = AVAudioPlayerNode()
        attachNodes()
        recover(why)
    }

    private func restoreAfterStyle() {
        engine.stop()
        do {
            try session.setActive(false, options: .notifyOthersOnDeactivation)
            try holdQuietSession()
            try startEngine()
            switching = false
        } catch {
            switching = false
            log("PROBLEM restoring after a sound: \(error.localizedDescription)")
            recover("after a sound")
        }
        if rebuildAfterSwitch {
            rebuildAfterSwitch = false
            rebuild("format change during a sound")
        }
    }

    private func start(_ kind: SoundKind, then done: (() -> Void)?) -> String? {
        if !engine.isRunning && !switching { recover("before a sound") }
        guard engine.isRunning, let buffer = sounds[kind] else { return "the audio engine is stopped" }
        // Dropped, not queued: a sound that would play late is worse than none, as with the gaps.
        guard soundsUnderway < Self.maxSoundsUnderway else { return "two sounds are already playing" }
        let sentGeneration = generation
        let startedAt = Date()
        let duration = Double(buffer.frameLength) / format.sampleRate
        if soundsUnderway == 0 { _ = takePeak() }   // never in the middle of another sound's measurement
        soundsUnderway += 1
        player.volume = volume
        player.scheduleBuffer(buffer, at: nil, options: [], completionCallbackType: .dataPlayedBack) { [weak self] _ in
            DispatchQueue.main.async {
                guard let self else { return }
                let elapsed = Date().timeIntervalSince(startedAt)
                self.soundsUnderway = max(0, self.soundsUnderway - 1)
                let peak = self.soundsUnderway == 0 ? self.takePeak() : self.currentPeak()
                let level = peak > 0 ? 20 * log10(Double(peak)) : -120
                let silentByChoice = self.volume < 0.05
                if sentGeneration == self.generation && elapsed >= duration * 0.85 && (peak >= 0.02 || silentByChoice) {
                    self.soundsPlayed += 1
                    self.lastPeakDB = level
                    self.onResult(.played(kind, peakDB: level))
                } else {
                    self.onResult(.failed(kind, String(format: "cut off or silent: %.2f s, peak %.1f dBFS", elapsed, level)))
                }
                done?()
            }
        }
        player.play()
        return nil
    }

    private func startWatchdog() {
        watchdog?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 1, repeating: 1)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            if !self.interrupted && !self.switching && !self.engine.isRunning {
                self.log("PROBLEM audio engine found stopped")
                self.recover("engine stopped")
            }
            // Apple warns the end-of-interruption signal may never arrive. Silence nobody
            // notices is the worst failure here, so stop waiting for it.
            let now = Date()
            if self.interrupted, let since = self.interruptedAt, now.timeIntervalSince(since) > 30,
               now.timeIntervalSince(self.lastStuckAttempt) > 10 {
                self.lastStuckAttempt = now
                self.log("no end of interruption after 30 s: trying anyway")
                self.recover("no interruption end")
            }
        }
        timer.resume()
        watchdog = timer
    }

    // MARK: System events

    @objc private func interruption(_ notification: Notification) {
        guard let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        DispatchQueue.main.async {
            guard self.armed else { return }
            if type == .began {
                self.interrupted = true
                self.interruptedAt = Date()
                self.log("audio interrupted (call or Siri)")
                self.onStateChange()
            } else {
                // Deliberately ignores shouldResume: that advice is about resuming a person's
                // media, and an alert app that stays silent after a call is broken.
                self.log("audio interruption ended")
                self.recover("interruption ended")
            }
        }
    }

    @objc private func routeChanged(_ notification: Notification) {
        let reason = notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt ?? 0
        DispatchQueue.main.async {
            self.log("audio output is now \(self.routeName) (reason \(reason))")
            if self.armed && !self.switching && !self.interrupted && !self.engine.isRunning {
                self.recover("route change")
            }
            self.onStateChange()
        }
    }

    @objc private func engineChanged(_ notification: Notification) {
        // Apple: this arrives on an internal queue, and tearing the engine down there can
        // deadlock. Moving to the main queue first is what makes the rebuild safe.
        DispatchQueue.main.async {
            guard (notification.object as AnyObject?) === self.engine else { return }
            self.log("audio format changed")
            if self.switching {
                self.rebuildAfterSwitch = true
            } else {
                self.rebuild("format change")
            }
        }
    }

    @objc private func mediaReset() {
        DispatchQueue.main.async {
            self.log("media services reset")
            self.rebuild("media reset")
        }
    }

    // MARK: Sounds

    /// About -80 dBFS of noise: inaudible, but real audio rather than digital zero.
    private static func makeQuietLoop(_ format: AVAudioFormat) -> AVAudioPCMBuffer {
        let frames = AVAudioFrameCount(format.sampleRate)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
        buffer.frameLength = frames
        let samples = buffer.floatChannelData![0]
        for i in 0..<Int(frames) { samples[i] = Float.random(in: -0.0001...0.0001) }
        return buffer
    }

    static func buffer(_ samples: [Float], _ format: AVAudioFormat) -> AVAudioPCMBuffer {
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(max(1, samples.count)))!
        buffer.frameLength = AVAudioFrameCount(samples.count)
        let out = buffer.floatChannelData![0]
        for (i, value) in samples.enumerated() { out[i] = value }
        return buffer
    }

    /// Replaces one sound. Used when the server's settings describe new sounds.
    func setSound(_ kind: SoundKind, samples: [Float]) {
        guard !samples.isEmpty else { return }
        sounds[kind] = Self.buffer(samples, format)
    }

    /// A WAV file from the server, turned into this engine's format: mono, 44.1 kHz, at most 4 s.
    func samples(fromWAV data: Data) -> [Float]? {
        #if DEBUG
        // Decoding also wakes CoreAudio. The download is still tested; the decoding is not, on such a Mac.
        if Self.skippedForTests {
            return data.starts(with: Data("RIFF".utf8)) ? [0.5] : nil
        }
        #endif
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".wav")
        defer { try? FileManager.default.removeItem(at: url) }
        guard (try? data.write(to: url)) != nil, let file = try? AVAudioFile(forReading: url),
              file.length > 0, Double(file.length) / file.processingFormat.sampleRate <= 4.0,
              let input = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length)),
              (try? file.read(into: input)) != nil,
              let converter = AVAudioConverter(from: file.processingFormat, to: format) else { return nil }
        let capacity = AVAudioFrameCount(Double(input.frameLength) * format.sampleRate / file.processingFormat.sampleRate + 1024)
        guard let output = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return nil }
        var fed = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if fed { status.pointee = .endOfStream; return nil }
            fed = true
            status.pointee = .haveData
            return input
        }
        guard error == nil, output.frameLength > 0, let channel = output.floatChannelData?[0] else { return nil }
        let samples = Array(UnsafeBufferPointer(start: channel, count: Int(output.frameLength)))
        let top = samples.map(abs).max() ?? 0
        return top > 0.01 ? samples : nil
    }
}
