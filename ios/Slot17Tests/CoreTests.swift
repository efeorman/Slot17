import XCTest

final class ProtocolTests: XCTestCase {
    private func message(_ json: String) throws -> ServerMessage {
        try JSONDecoder().decode(ServerMessage.self, from: Data(json.utf8))
    }

    func testAMessageFromAnOlderPublisherDecodes() throws {
        let m = try message(#"{"boot":"b","seq":1,"server_mono_s":5,"server_age_s":0.5,"state":{"v":3,"source":"x","snapshot_age_s":0.1,"seq":1,"session":"s","detector":{"state":"running"},"channels":[],"matches":[]}}"#)
        XCTAssertEqual(m.state?.detector.state, "running")
        XCTAssertNil(m.state?.pi)
        XCTAssertNil(m.state?.features)
    }

    func testABeforeAnyPushMessageDecodes() throws {
        let m = try message(#"{"boot":"b","seq":0,"server_mono_s":5,"server_age_s":null,"state":null}"#)
        XCTAssertNil(m.state)
        XCTAssertNil(m.serverAge)
    }

    func testAFullMessageDecodes() throws {
        let m = try message(#"{"boot":"b","seq":2,"server_mono_s":5.5,"server_age_s":0.2,"command":{"action":"stop","state":"sent","age_s":1},"ops":[{"id":"a","kind":"mark","state":"done","detail":"Saved","age_s":1}],"state":{"snapshot_age_s":0.1,"session":"s","source":"x","features":["mark"],"pi":{"temp_c":51.5,"session_s":120},"detector":{"state":"degraded","reasons":["x"],"quiet_windows":3,"input_peak_counts":12.5},"channels":[{"ch":7,"id":"s/a","freq_mhz":380.2,"strength_db":null,"age_s":1,"hits_60s":2,"activity":null,"approx_time":true}],"matches":[{"id":"s/a","ch":7,"strength_db":30,"age_s":1,"delay_s":1}]}}"#)
        XCTAssertEqual(m.state?.pi?.temperature, 51.5)
        XCTAssertEqual(m.state?.channels.first?.hits60, 2)
        XCTAssertNil(m.state?.channels.first?.strength)
        XCTAssertEqual(m.ops?.first?.detail, "Saved")
        XCTAssertEqual(m.command?.state, "sent")
    }
}

final class SSEParserTests: XCTestCase {
    func testAMessageSplitAcrossChunksIsJoined() {
        var parser = SSEParser()
        XCTAssertEqual(parser.feed(Data("retry: 2000\n\ndata: {\"a\"".utf8)), [])
        XCTAssertEqual(parser.feed(Data(":1}\n\ndata: {\"b\":2}\n\n".utf8)), ["{\"a\":1}", "{\"b\":2}"])
    }

    func testItCannotGrowWithoutLimit() {
        var parser = SSEParser()
        _ = parser.feed(Data(repeating: 65, count: 3_000_000))
        XCTAssertEqual(parser.feed(Data("data: ok\n\n".utf8)), ["ok"])
    }
}

final class ServerLinkTests: XCTestCase {
    func testThePrivateLinkIsSplit() throws {
        let link = try XCTUnwrap(ServerLink.parse("  http://192.0.2.10:5732/?key=abcDEF123456\n"))
        XCTAssertEqual(link.base.absoluteString, "http://192.0.2.10:5732")
        XCTAssertEqual(link.key, "abcDEF123456")
    }

    func testAnythingElseIsRefused() {
        for bad in ["", "hello", "http://192.0.2.10:5732/", "ftp://192.0.2.10/?key=abcdefgh",
                    "http://192.0.2.10/?key=short", "?key=abcdefgh"] {
            XCTAssertNil(ServerLink.parse(bad), bad)
        }
    }
}

final class LinkTrackerTests: XCTestCase {
    private func message(boot: String = "b", seq: Int, mono: Double, carAge: Double = 0.1, serverAge: Double = 0.1,
                         detector: String = "running", session: String = "s") -> ServerMessage {
        ServerMessage(boot: boot, seq: seq, serverMono: mono, serverAge: serverAge,
                      state: PiState(source: "x", session: session, snapshotAge: carAge,
                                     detector: DetectorState(state: detector), channels: [], matches: []),
                      command: nil, ops: nil)
    }

    func testOlderAndDuplicateMessagesAreRefused() {
        let t = LinkTracker()
        XCTAssertEqual(t.applyProbe(message(seq: 5, mono: 1000), begin: 10, end: 10.1), .freshAndNewScope)
        XCTAssertEqual(t.accept(message(seq: 4, mono: 1001), now: 11), .refused)
        XCTAssertEqual(t.accept(message(seq: 5, mono: 1000), now: 11), .refused)
        XCTAssertEqual(t.accept(message(seq: 5, mono: 1001), now: 11), .repeated)
        XCTAssertEqual(t.accept(message(seq: 6, mono: 1002), now: 12), .fresh)
    }

    func testANewServerBootNeedsATimingSampleFirst() {
        let t = LinkTracker()
        t.applyProbe(message(seq: 5, mono: 1000), begin: 10, end: 10.1)
        XCTAssertEqual(t.accept(message(boot: "new", seq: 1, mono: 5), now: 11), .refused)
        XCTAssertEqual(t.applyProbe(message(boot: "new", seq: 1, mono: 5), begin: 11, end: 11.1), .freshAndNewScope)
        XCTAssertEqual(t.accept(message(seq: 9, mono: 2000), now: 12), .refused, "a retired boot cannot return")
    }

    func testASlowTimingSampleIsNotUsed() {
        let t = LinkTracker()
        XCTAssertEqual(t.applyProbe(message(seq: 1, mono: 1000), begin: 10, end: 14.5), .refused)
        XCTAssertNil(t.clock)
    }

    func testLiveNeedsEveryLink() {
        let t = LinkTracker()
        XCTAssertFalse(t.live(10))
        t.applyProbe(message(seq: 1, mono: 1000), begin: 10, end: 10.05)
        XCTAssertTrue(t.live(10.1))
        XCTAssertFalse(t.live(10.05 + LinkTracker.linkLost + 1), "phone link lost")
        t.authFailed = true
        XCTAssertFalse(t.live(10.1), "key refused")
    }

    func testAgesAreUpperBoundsAndGrow() {
        let t = LinkTracker()
        t.applyProbe(message(seq: 1, mono: 1000, serverAge: 0.5), begin: 10, end: 10.2)
        let f = t.frame!
        let first = t.elapsed(f, 10.2)
        XCTAssertGreaterThanOrEqual(first, 0.5 + 0.2)
        XCTAssertEqual(t.elapsed(f, 11.2) - first, 1.0, accuracy: 0.01)
    }

    func testATimingSampleExpires() {
        let t = LinkTracker()
        t.applyProbe(message(seq: 1, mono: 1000), begin: 10, end: 10.1)
        XCTAssertTrue(t.clockReady(10.05 + LinkTracker.clockTTL))
        XCTAssertFalse(t.clockReady(10.2 + LinkTracker.clockTTL))
        XCTAssertEqual(t.elapsed(t.frame!, 10.2 + LinkTracker.clockTTL), .infinity)
    }

    func testAnOldCarUpdateIsNotLive() {
        let t = LinkTracker()
        t.applyProbe(message(seq: 1, mono: 1000, carAge: LinkTracker.carStale + 1), begin: 10, end: 10.1)
        XCTAssertFalse(t.live(10.2))
    }

    func testAStoppedDetectorIsNotLive() {
        let t = LinkTracker()
        t.applyProbe(message(seq: 1, mono: 1000, detector: "stopped"), begin: 10, end: 10.1)
        XCTAssertFalse(t.live(10.2))
        XCTAssertFalse(t.mayHoldMatches(10.2))
    }

    func testAChangedSessionIsANewScope() {
        let t = LinkTracker()
        t.applyProbe(message(seq: 1, mono: 1000, session: "one"), begin: 10, end: 10.1)
        XCTAssertEqual(t.accept(message(seq: 2, mono: 1001, session: "two"), now: 11), .freshAndNewScope)
    }

    /// 1.8.0: with both routes fresh, Bluetooth's data comes straight from the car computer and goes first.
    func testBluetoothGoesFirstWhileItsDataIsFresh() {
        XCTAssertTrue(LinkTracker.bluetoothFirst(live: true, age: 1.2, serverFresh: true), "both fresh")
        XCTAssertTrue(LinkTracker.bluetoothFirst(live: true, age: LinkTracker.bluetoothFirstWithin, serverFresh: true))
        XCTAssertFalse(LinkTracker.bluetoothFirst(live: true, age: 6, serverFresh: true), "a stalled radio link")
        XCTAssertTrue(LinkTracker.bluetoothFirst(live: true, age: 6, serverFresh: false), "nothing fresher")
        XCTAssertFalse(LinkTracker.bluetoothFirst(live: false, age: 0.5, serverFresh: true), "no running detector over it")
        XCTAssertFalse(LinkTracker.bluetoothFirst(live: false, age: 0.5, serverFresh: false))
    }
}

final class StatusTests: XCTestCase {
    private func tracked(detector: DetectorState, carAge: Double = 0.1, now: Double = 10.2) -> LinkTracker {
        let t = LinkTracker()
        t.applyProbe(ServerMessage(boot: "b", seq: 1, serverMono: 1000, serverAge: 0.1,
                                   state: PiState(source: "x", session: "s", snapshotAge: carAge, detector: detector,
                                                  channels: [], matches: []), command: nil, ops: nil),
                     begin: 10, end: 10.1)
        return t
    }

    func testPlainWords() {
        XCTAssertEqual(StatusBuilder.headline(LinkTracker(), now: 1, lastDelay: nil, hasCredentials: false).title, "Not set up")
        XCTAssertEqual(StatusBuilder.headline(LinkTracker(), now: 1, lastDelay: nil, hasCredentials: false, bluetoothSetUp: true).title,
                       "Waiting for the car computer")
        XCTAssertEqual(StatusBuilder.headline(LinkTracker(), now: 1, lastDelay: nil, hasCredentials: true).title, "Connecting…")
        XCTAssertEqual(StatusBuilder.headline(tracked(detector: DetectorState(state: "running")), now: 10.2, lastDelay: 3.21, hasCredentials: true).detail,
                       "Delay up to 3.3 s after the burst")
        XCTAssertEqual(StatusBuilder.headline(tracked(detector: DetectorState(state: "stale", heartbeatUnchanged: 12)), now: 10.2, lastDelay: nil, hasCredentials: true).title,
                       "Detector not responding")
        XCTAssertEqual(StatusBuilder.headline(tracked(detector: DetectorState(state: "running"), carAge: 20), now: 10.2, lastDelay: nil, hasCredentials: true).title,
                       "No data from the car")
    }

    func testTheAircraftChannelsAre190To199() {
        // ECC/DEC/(06)05: 384.750-385.000 MHz is channels 190-199 on the grid (1.8.0).
        let air = (0..<200).filter { ch in let f = 380.0125 + Double(ch) * 0.025; return f >= 384.75 - 1e-9 && f < 385.0 }
        XCTAssertEqual(air, (0..<200).filter { Aircraft.isAir($0) })
        XCTAssertTrue(MeterRow(ch: 190, freqMHz: 384.7625, strength: 30, age: 1, hits60: 1, approxTime: false).air)
        XCTAssertFalse(MeterRow(ch: 189, freqMHz: 384.7375, strength: 30, age: 1, hits60: 1, approxTime: false).air)
        XCTAssertFalse(Aircraft.isAir(nil))
    }

    func testAircraftSoundOffSilencesOnlyTheAircraftChannels() {
        var s = AlertSettings()
        XCTAssertTrue(s.aircraftSound, "on by default: nothing changes unless the operator switches it off")
        s.aircraftSound = false
        let engine = AlertEngine()
        var played: [SoundKind] = []
        func run(_ ch: Int, _ id: String, _ now: Double) -> AlertDecision.Outcome? {
            engine.ingest([MatchRow(id: id, ch: ch, strength: 40, age: 1)], frame: nil, channels: [], scope: "s")
            return engine.flush(AlertEngine.Context(now: now, live: true, mayHold: false, scope: "s", settings: s, armed: true,
                                                    audioProblem: nil, mutedUntil: -1, age: { $0.match.age },
                                                    play: { played.append($0); return nil })).first?.outcome
        }
        XCTAssertEqual(run(190, "a", 100), .silent("an aircraft channel, and sound for aircraft is off"))
        XCTAssertEqual(run(189, "g", 101), .played(.chime))
        XCTAssertEqual(played, [.chime])
        let saved = try! JSONEncoder().encode(s)
        XCTAssertFalse(try! JSONDecoder().decode(AlertSettings.self, from: saved).aircraftSound, "the choice is kept")
    }

    func testTheChainNamesTheBrokenLink() {
        let healthy = StatusBuilder.chain(tracked(detector: DetectorState(state: "running")), now: 10.2, hasCredentials: true)
        XCTAssertEqual(healthy.map(\.level), [.ok, .ok, .ok, .ok])

        let piSilent = StatusBuilder.chain(tracked(detector: DetectorState(state: "running"), carAge: 20), now: 10.2, hasCredentials: true)
        XCTAssertEqual(piSilent.map(\.level), [.ok, .bad, .idle, .idle])

        let deaf = StatusBuilder.chain(tracked(detector: DetectorState(state: "running", quietWindows: 240)), now: 10.2, hasCredentials: true)
        XCTAssertEqual(deaf[3].detail, "Hears nothing")

        let unreachable = StatusBuilder.chain(LinkTracker(), now: 1, hasCredentials: true)
        XCTAssertEqual(unreachable.map(\.level), [.bad, .idle, .idle, .idle])
    }

    func testTheBarIsFullOnlyAtTheUrgentLevel() {
        XCTAssertEqual(Meters.level(nil), 0)
        XCTAssertEqual(Meters.level(0), 1, "every detection lights at least one segment")
        XCTAssertEqual(Meters.level(6.5), 1)
        XCTAssertEqual(Meters.level(12), 2)
        XCTAssertEqual(Meters.level(22), 4)
        XCTAssertEqual(Meters.level(36), 7)
        XCTAssertEqual(Meters.level(49.9), 9, "the top segment means the urgent level was reached")
        XCTAssertEqual(Meters.level(50), 10)
        XCTAssertEqual(Meters.level(100), 10)
        XCTAssertLessThan(Meters.level(36), Meters.level(59), "36 and 59 dB no longer look the same")
        XCTAssertEqual(Meters.level(40, urgent: 40), 10, "follows the urgent level from the server")
        XCTAssertTrue((0...200).map { Meters.level(Double($0) / 2) }.adjacentPairsAreSorted)
    }

    func testMeterColoursFollowWhatTheAppDoes() {
        var s = AlertSettings()
        XCTAssertEqual(Meters.colour(35, s), .red, "the chime level is the server's 35 dB by default")
        XCTAssertEqual(Meters.colour(34.9, s), .yellow)
        XCTAssertEqual(Meters.colour(11.9, s), .weak, "never green")
        XCTAssertEqual(Meters.colour(nil, s), .grey)
        XCTAssertFalse(Meters.isStrong(49.9, s))
        XCTAssertTrue(Meters.isStrong(50, s))
        s.quiet = true
        XCTAssertEqual(Meters.colour(25, s), .yellow, "in Quiet mode 25 dB does not chime, so it is not red")
    }

    func testMetersPutRecentChannelsFirstThenStrongest() {
        let row = { (ch: Int, db: Double?, age: Double) in
            MeterRow(ch: ch, freqMHz: 380, strength: db, age: age, hits60: 1, approxTime: false)
        }
        let picked = Meters.pick([row(1, 60, 40), row(2, 15, 5), row(3, 30, 5), row(4, 99, 61), row(5, nil, 2)])
        XCTAssertEqual(picked.map(\.ch), [3, 2, 5, 1])
    }
}

final class SightingTests: XCTestCase {
    private func sighting() -> Sighting {
        Sighting(id: Sighting.newID(), label: .police, note: "", seenAt: Date(), phase: .sending, detail: "", isRecordingRequest: false)
    }

    func testIdsAreWhatTheServerAccepts() {
        XCTAssertNotNil(Sighting.newID().range(of: "^[0-9a-f]{32}$", options: .regularExpression))
    }

    func testItStaysQueuedUntilThePiAnswers() {
        var s = sighting()
        XCTAssertTrue(s.needsSending)
        s.apply(state: "pending", detail: "")
        XCTAssertTrue(s.needsSending)
        s.apply(state: "sent", detail: "")
        XCTAssertEqual(s.statusText, "The Pi has it")
        XCTAssertTrue(s.needsSending)
        s.apply(state: "done", detail: "Saved with recording (queued)")
        XCTAssertFalse(s.needsSending)
        XCTAssertEqual(s.statusText, "Saved with recording (queued)")
    }

    func testFailureIsShown() {
        var s = sighting()
        s.apply(state: "failed", detail: "Not saved on the Pi: no running session")
        XCTAssertEqual(s.phase, .failed)
        XCTAssertFalse(s.needsSending)
    }

    func testOnlyLabelsThePiAcceptsExist() {
        let accepted: Set<String> = ["police", "ambulance", "fire", "unknown", "noise", "antenna-change"]
        for label in Sighting.Label.allCases { XCTAssertTrue(accepted.contains(label.rawValue), label.rawValue) }
    }
}

private extension Array where Element == Int {
    var adjacentPairsAreSorted: Bool { zip(self, dropFirst()).allSatisfy { $0 <= $1 } }

    func testTheConnectionBlockDecodesAndReadsInPlainWords() throws {
        let json = #"{"via":"4G stick","networks":["4G stick","Wi-Fi"],"device":"eth1","pushes":30,"failed":1,"median_ms":52,"slowest_ms":140,"clock_synced":true,"stick":{"connected":true,"network":"LTE","provider":"LycaMobile","roaming":true,"bars":3,"rsrp":-107,"rsrq":-12,"snr":-1,"cell":"24c315/177","ping_ms":48.2,"ping_worst_ms":90.1,"ping_lost":0}}"#
        let link = try JSONDecoder().decode(PiLink.self, from: Data(json.utf8))
        XCTAssertEqual(link.via, "4G stick")
        XCTAssertEqual(link.stick?.rsrp, -107)
        XCTAssertEqual(link.stick?.pingMS, 48.2)
        XCTAssertEqual(link.viaText, "the 4G stick (eth1)")
        XCTAssertEqual(SignalWords.rsrp(-85), "good")
        XCTAssertEqual(SignalWords.rsrp(-107), "weak")
        XCTAssertEqual(SignalWords.rsrp(-120), "very weak")
        XCTAssertEqual(SignalWords.wifi(-55), "good")
        // An older Pi sends no block at all; the state still decodes.
        XCTAssertNil(try JSONDecoder().decode(PiLink.self, from: Data("{}".utf8)).via)
    }
}

final class NotifySwitchTests: XCTestCase {
    func testThePushoverSwitchDecodesAndIsOptional() throws {
        let with = #"{"boot":"b","seq":1,"server_mono_s":5,"state":{"snapshot_age_s":0,"detector":{"state":"running"},"channels":[],"matches":[],"features":["notify"],"notify":{"pushover":false,"configured":true}}}"#
        let m = try JSONDecoder().decode(ServerMessage.self, from: Data(with.utf8))
        XCTAssertEqual(m.state?.notify, NotifySwitch(pushover: false, configured: true))
        let without = #"{"boot":"b","seq":1,"server_mono_s":5,"state":{"snapshot_age_s":0,"detector":{"state":"running"},"channels":[],"matches":[]}}"#
        XCTAssertNil(try JSONDecoder().decode(ServerMessage.self, from: Data(without.utf8)).state?.notify)
    }
}

final class RuleAndVersionTests: XCTestCase {
    func testTheRuleAndVersionDecodeAndOlderRecordsStillLoad() throws {
        let json = #"{"boot":"b","seq":1,"server_mono_s":5,"state":{"snapshot_age_s":0,"software":"1.2.9","detector":{"state":"running"},"channels":[],"matches":[{"id":"a","ch":7,"strength_db":30,"age_s":1,"rule":"coherent"},{"id":"b","ch":8,"strength_db":20,"age_s":1}]}}"#
        let m = try JSONDecoder().decode(ServerMessage.self, from: Data(json.utf8))
        XCTAssertEqual(m.state?.software, "1.2.9")
        XCTAssertEqual(m.state?.matches.map(\.rule), ["coherent", nil])
        let old = #"[{"id":"x","at":0,"channel":7,"strength":30,"age":1,"reason":""}]"#
        let records = try JSONDecoder().decode([AlertRecord].self, from: Data(old.utf8))
        XCTAssertNil(records.first?.rule)
    }
}
