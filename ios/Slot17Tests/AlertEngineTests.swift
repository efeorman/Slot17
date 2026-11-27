import XCTest

/// Holds the app to the rule already checked against the web page in a real browser.
final class AlertEngineTests: XCTestCase {
    private var played: [SoundKind] = []
    private var settings = AlertSettings()
    private var engine = AlertEngine()

    override func setUp() {
        played = []
        settings = AlertSettings()
        // These tests are about rules relative to the chime level, written at 22 dB; the default is 35 since 27
        // November 2026 (testTheDefaultChimeLevelIsTheServers).
        settings.red = 22
        engine = AlertEngine()
    }

    private func match(_ id: String, ch: Int = 1, db: Double?, age: Double = 1) -> MatchRow {
        MatchRow(id: id, ch: ch, strength: db, age: age)
    }

    private func context(now: Double, live: Bool = true, mayHold: Bool = false, armed: Bool = true,
                         problem: String? = nil, mutedUntil: Double = -1, scope: String? = "s",
                         refuse: String? = nil) -> AlertEngine.Context {
        AlertEngine.Context(
            now: now, live: live, mayHold: mayHold, scope: scope, settings: settings, armed: armed,
            audioProblem: problem, mutedUntil: mutedUntil, age: { $0.match.age },
            play: { [weak self] kind in
                if let refuse { return refuse }
                self?.played.append(kind)
                return nil
            })
    }

    private func run(_ matches: [MatchRow], now: Double, channels: [ChannelRow] = [], ctx: AlertEngine.Context? = nil) -> [AlertDecision] {
        engine.ingest(matches, frame: nil, channels: channels, scope: "s")
        return engine.flush(ctx ?? context(now: now))
    }

    // MARK: Real drives

    /// Every saved drive, window by window, must give exactly the counts the replay tool gives.
    func testAllFourSavedDrivesGiveTheSameSoundCounts() throws {
        struct Drive: Decodable {
            let chimes: Int
            let urgent: Int
            let rows: [Row]
        }
        struct Row: Decodable {
            let id: String, ch: Int, burst: Double, window: Int, arrive: Double, snr: Double?
            init(from decoder: Decoder) throws {
                var c = try decoder.unkeyedContainer()
                id = try c.decode(String.self)
                ch = try c.decode(Int.self)
                burst = try c.decode(Double.self)
                window = try c.decode(Int.self)
                arrive = try c.decode(Double.self)
                snr = try c.decodeIfPresent(Double.self)
            }
        }
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "drives", withExtension: "json"))
        let drives = try JSONDecoder().decode([String: Drive].self, from: Data(contentsOf: url))
        XCTAssertEqual(drives.count, 4)
        for (name, drive) in drives {
            engine = AlertEngine()
            played = []
            let windows = Dictionary(grouping: drive.rows, by: \.window)
            for number in windows.keys.sorted() {
                let rows = windows[number]!
                let now = rows[0].arrive
                let matches = rows.map { match($0.id, ch: $0.ch, db: $0.snr, age: now - $0.burst) }
                _ = run(matches, now: now)
            }
            XCTAssertEqual(played.filter { $0 == .chime }.count, drive.chimes, "\(name) chimes")
            XCTAssertEqual(played.filter { $0 == .urgent }.count, drive.urgent, "\(name) urgent")
        }
    }

    // MARK: Levels

    func testFiftyDecibelsIsUrgentAndJustUnderIsNot() {
        _ = run([match("a", db: 50.0)], now: 100)
        XCTAssertEqual(played, [.urgent])
        played = []
        _ = run([match("b", ch: 2, db: 49.9)], now: 200)
        XCTAssertEqual(played, [.chime])
    }

    func testAChimeAMomentEarlierDoesNotSilenceAStrongSignal() {
        _ = run([match("a", ch: 1, db: 30)], now: 100)
        let second = run([match("b", ch: 2, db: 51.2)], now: 100.014)
        XCTAssertEqual(played, [.chime, .urgent])
        XCTAssertEqual(second.first?.outcome, .played(.urgent))
    }

    func testTwoUrgentBeepsStillKeepTheirGap() {
        _ = run([match("a", ch: 1, db: 55)], now: 100)
        _ = run([match("b", ch: 2, db: 55)], now: 100.5)
        XCTAssertEqual(played, [.urgent])
    }

    func testTheDefaultChimeLevelIsTheServers() {
        // display/app-config.json's red_db: without a server the app chimed from 22 dB until 27 November 2026.
        XCTAssertEqual(AlertSettings().red, 35)
        XCTAssertEqual(AlertSettings().receiverQuietWindows, 240)
    }

    func testRedLevelBoundary() {
        XCTAssertEqual(run([match("a", db: 21.9)], now: 100).first?.outcome, .silent("21.9 dB is below the 22 dB red level"))
        XCTAssertEqual(played, [])
        _ = run([match("b", db: 22.0)], now: 100)
        XCTAssertEqual(played, [.chime])
    }

    func testAMatchWithNoStrengthIsSilent() {
        XCTAssertEqual(run([match("a", db: nil)], now: 100).first?.outcome, .silent("the match had no usable strength"))
    }

    func testUrgentOffGivesTheNormalChimeAndItsLimits() {
        settings.urgent = false
        _ = run([match("a", db: 60)], now: 100)
        _ = run([match("b", db: 60)], now: 102)
        XCTAssertEqual(played, [.chime])
    }

    // MARK: Limits

    func testUrgentSkipsTheChimeLimitsButKeepsTheOneSecondGap() {
        _ = run([match("a", db: 55)], now: 100)
        _ = run([match("b", db: 55)], now: 100.5)
        _ = run([match("c", db: 55)], now: 101)
        XCTAssertEqual(played, [.urgent, .urgent])
    }

    func testChimeLimits() {
        _ = run([match("a", ch: 1, db: 30)], now: 100)
        XCTAssertEqual(run([match("b", ch: 2, db: 30)], now: 105).first?.outcome, .silent("another chime played under 6 s ago"))
        XCTAssertEqual(run([match("c", ch: 1, db: 30)], now: 110).first?.outcome, .silent("this channel chimed under 30 s ago"))
        _ = run([match("d", ch: 2, db: 30)], now: 106.5)
        _ = run([match("e", ch: 1, db: 30)], now: 130)
        XCTAssertEqual(played, [.chime, .chime, .chime])
    }

    func testUrgentGoesFirstInTheSamePush() {
        _ = run([match("a", ch: 1, db: 30), match("b", ch: 2, db: 55)], now: 100)
        XCTAssertEqual(played, [.urgent])
    }

    func testADuplicateNeverSoundsTwice() {
        _ = run([match("a", db: 55)], now: 100)
        _ = run([match("a", db: 55)], now: 200)
        XCTAssertEqual(played, [.urgent])
    }

    func testTheSeenListIsBoundedButRecentIdsAreRemembered() {
        for n in 0..<450 { engine.ingest([match("m\(n)", db: 1)], frame: nil, channels: [], scope: "s") }
        _ = engine.flush(context(now: 100))
        _ = run([match("m449", db: 55)], now: 200)
        XCTAssertEqual(played, [])
        _ = run([match("m0", db: 55)], now: 300)
        XCTAssertEqual(played, [.urgent])
    }

    // MARK: Freshness and link

    func testAStaleMatchIsDropped() {
        XCTAssertEqual(run([match("a", db: 55, age: 10.1)], now: 100).first?.outcome,
                       .silent("it reached the phone 10.1 s old (limit 10 s)"))
        XCTAssertEqual(played, [])
    }

    func testNothingPlaysOrWaitsWhenNotLive() {
        XCTAssertEqual(run([match("a", db: 55)], now: 100, ctx: context(now: 100, live: false)), [])
        XCTAssertEqual(engine.waitingCount, 0)
        XCTAssertEqual(engine.flush(context(now: 101)), [])
        XCTAssertEqual(played, [])
    }

    func testAMatchMayWaitOnlyForTheFirstClockSample() {
        _ = run([match("a", db: 55)], now: 100, ctx: context(now: 100, live: false, mayHold: true))
        XCTAssertEqual(engine.waitingCount, 1)
        _ = engine.flush(context(now: 101))
        XCTAssertEqual(played, [.urgent])
    }

    func testAMatchFromAnEarlierSessionIsSilent() {
        engine.ingest([match("a", db: 55)], frame: nil, channels: [], scope: "old")
        XCTAssertEqual(engine.flush(context(now: 100)).first?.outcome, .silent("the link or the detector was not live"))
        XCTAssertEqual(played, [])
    }

    // MARK: Sound state

    func testMuteSoundOffAndUnarmed() {
        XCTAssertEqual(run([match("a", db: 55)], now: 100, ctx: context(now: 100, mutedUntil: 130)).first?.outcome,
                       .silent("muted by the speaker button"))
        settings.sound = false
        XCTAssertEqual(run([match("b", db: 55)], now: 200).first?.outcome, .silent("sound is switched off"))
        settings.sound = true
        XCTAssertEqual(run([match("c", db: 55)], now: 300, ctx: context(now: 300, armed: false)).first?.outcome,
                       .silent("sound is not armed"))
        XCTAssertEqual(played, [])
    }

    func testAnAudioProblemIsNamed() {
        XCTAssertEqual(run([match("a", db: 55)], now: 100, ctx: context(now: 100, problem: "a call is using the audio")).first?.outcome,
                       .silent("a call is using the audio"))
    }

    func testARefusedSoundDoesNotUseUpTheGap() {
        XCTAssertEqual(run([match("a", db: 55)], now: 100, ctx: context(now: 100, refuse: "the audio is busy")).first?.outcome,
                       .silent("the audio is busy"))
        _ = run([match("b", db: 55)], now: 100.1)
        XCTAssertEqual(played, [.urgent])
    }

    func testRepeatNeeded() {
        settings.repeatNeeded = true
        let once = ChannelRow(ch: 1, freqMHz: 380, strength: 30, age: 1, hits60: 1, approxTime: false)
        let twice = ChannelRow(ch: 1, freqMHz: 380, strength: 30, age: 1, hits60: 2, approxTime: false)
        _ = run([match("a", db: 30)], now: 100, channels: [once])
        XCTAssertEqual(played, [])
        _ = run([match("b", db: 30)], now: 100, channels: [twice])
        XCTAssertEqual(played, [.chime])
    }

    func testTheReasonClearsAfterASound() {
        _ = run([match("a", db: 5)], now: 100)
        XCTAssertFalse(engine.lastSilenceReason.isEmpty)
        _ = run([match("b", db: 55)], now: 101)
        XCTAssertEqual(engine.lastSilenceReason, "")
    }

    // MARK: Call tone

    private func call(_ id: String, ch: Int = 1, db: Double, age: Double = 1) -> MatchRow {
        MatchRow(id: id, ch: ch, strength: db, age: age, call: true)
    }

    func testACallBelowTheChimeLevelGetsTheSoftToneOncePerChannel() {
        settings.red = 35
        XCTAssertEqual(run([call("a", db: 25)], now: 100).first?.outcome, .played(.call))
        XCTAssertEqual(played, [.call])
        // The same channel again within its gap: silent, with the reason.
        if case .silent(let why) = run([call("b", db: 26)], now: 130).first?.outcome {
            XCTAssertTrue(why.contains("call tone"), why)
        } else { XCTFail("expected silence") }
        // Another channel after the overall gap: plays.
        XCTAssertEqual(run([call("c", ch: 2, db: 25)], now: 120).first?.outcome, .played(.call))
    }

    func testOnlyACallPatternGetsTheToneAndTheChimeStillRules() {
        settings.red = 35
        XCTAssertNotEqual(run([match("a", db: 25)], now: 100).first?.outcome, .played(.call), "no call pattern, below red")
        let low = run([call("b", ch: 3, db: 12)], now: 200).first?.outcome
        XCTAssertNotEqual(low, .played(.call), "below the call level")
        XCTAssertNotEqual(low, .played(.chime))
        XCTAssertEqual(run([call("c", ch: 4, db: 40)], now: 300).first?.outcome, .played(.chime), "at the chime level it is a chime")
        XCTAssertEqual(run([call("d", ch: 5, db: 55)], now: 400).first?.outcome, .played(.urgent))
    }

    func testACallToneNeverHoldsBackAChime() {
        settings.red = 35
        XCTAssertEqual(run([call("a", db: 25)], now: 100).first?.outcome, .played(.call))
        XCTAssertEqual(run([match("b", ch: 9, db: 40)], now: 102).first?.outcome, .played(.chime))
    }

    func testTheServerCanMoveOrSwitchOffTheCallTone() throws {
        let json = #"{"version":"v","config":{"rules":{"call_db":99,"call_gap_channel_s":120}}}"#
        let config = try XCTUnwrap(RemoteConfig.decode(Data(json.utf8))).config
        XCTAssertEqual(config.problems, [])
        settings.red = 35
        settings.apply(config)
        XCTAssertEqual(settings.callLevel, 99)
        XCTAssertEqual(settings.callGap, 120)
        XCTAssertNotEqual(run([call("a", db: 30)], now: 100).first?.outcome, .played(.call))
        let bad = #"{"version":"v","config":{"rules":{"call_db":2}}}"#
        XCTAssertFalse(try XCTUnwrap(RemoteConfig.decode(Data(bad.utf8))).config.problems.isEmpty)
        XCTAssertNil(SoundKind.call.builtIn.problem)
        XCTAssertLessThan(SoundKind.call.builtIn.peak, SoundKind.chime.builtIn.peak, "softer than the chime")
    }
}
