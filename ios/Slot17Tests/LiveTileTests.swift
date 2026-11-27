import UIKit
import XCTest

final class LiveTileBuilderTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    private func input(linkLost: Bool = false, running: Bool = true, quiet: Bool = false,
                       db: Double? = nil, age: Double = 2, frequent: Bool = false,
                       alert: AlertRecord? = nil, settings: AlertSettings = AlertSettings(),
                       at: Date? = nil) -> LiveTileBuilder.Input {
        let top = db.map { MeterRow(ch: 154, freqMHz: 383.86, strength: $0, age: age, hits60: 4, approxTime: false, frequent: frequent) }
        return .init(linkLost: linkLost, detectorRunning: running, receiverQuiet: quiet, top: top, lastAlert: alert,
                     settings: settings, style: DisplayStyle(), demo: false, now: at ?? now)
    }

    private func alert(_ sound: SoundKind?, secondsAgo: Double) -> AlertRecord {
        AlertRecord(id: "a", at: now.addingTimeInterval(-secondsAgo), channel: 101, strength: 54.6, age: 1,
                    sound: sound, reason: "")
    }

    func testCallRisingAndOtherChannelsReachTheTile() {
        var i = input(db: 30)
        i.top?.call = true
        i.top?.rising = true
        i.othersActive = 2
        let state = LiveTileBuilder.make(i)
        XCTAssertEqual(state.call, true)
        XCTAssertEqual(state.rising, true)
        XCTAssertEqual(state.others, 2)
        let plain = LiveTileBuilder.make(input(db: 30))
        XCTAssertNil(plain.call)
        XCTAssertNil(plain.others)
        XCTAssertTrue(LiveTilePolicy.isUrgent(plain, state), "a call starting goes at once")
    }

    func testAQuietTileKeepsTheLastChannelHeard() {
        let heard = LiveTileBuilder.make(input(db: 30, age: 2))
        var quiet = input()
        quiet.previous = heard
        let listening = LiveTileBuilder.make(quiet)
        XCTAssertEqual(listening.phase, .listening)
        XCTAssertEqual(listening.lastHeard?.channel, 154)
        XCTAssertEqual(listening.lastHeard?.at, heard.heardAt)
        var later = input(at: now.addingTimeInterval(60))
        later.previous = listening
        XCTAssertEqual(LiveTileBuilder.make(later).lastHeard, listening.lastHeard, "carried on while quiet")
        var muchLater = input(at: now.addingTimeInterval(31 * 60))
        muchLater.previous = listening
        XCTAssertNil(LiveTileBuilder.make(muchLater).lastHeard)
    }

    func testLateCarDataIsSaidAndSentAtOnce() {
        var i = input(db: 30)
        i.carAge = 1
        let fresh = LiveTileBuilder.make(i)
        XCTAssertNil(fresh.slow)
        i.carAge = 3
        XCTAssertNil(LiveTileBuilder.make(i).slow, "a 2 s keepalive plus transit is normal")
        i.carAge = 6
        let late = LiveTileBuilder.make(i)
        XCTAssertEqual(late.slow, true)
        XCTAssertTrue(LiveTilePolicy.isUrgent(fresh, late))
        i.previous = late
        i.carAge = 4
        XCTAssertEqual(LiveTileBuilder.make(i).slow, true, "stays late until clearly fresh again")
        i.carAge = 3
        XCTAssertNil(LiveTileBuilder.make(i).slow)
    }

    func testAMarkNamesTheChannelHeardNearItAndKeepsIt() {
        var i = input(db: 30, age: 12)
        i.lastMark = Sighting(id: "m", label: .police, note: "", seenAt: now, phase: .saved, detail: "",
                              isRecordingRequest: false, place: nil)
        let first = LiveTileBuilder.make(i)
        XCTAssertEqual(first.mark?.context, "L154 12 s before")
        var next = input(db: 40, age: 0, at: now.addingTimeInterval(5))
        next.top?.ch = 60
        next.lastMark = i.lastMark
        next.previous = first
        XCTAssertEqual(LiveTileBuilder.make(next).mark?.context, "L154 12 s before", "a later burst does not change it")
    }

    func testALouderColourGoesAtOnceAQuieterOneWaits() {
        var weak = LiveTileBuilder.make(input(db: 30))
        weak.tone = .silent
        var louder = weak
        louder.tone = .chime
        XCTAssertTrue(LiveTilePolicy.isUrgent(weak, louder))
        XCTAssertFalse(LiveTilePolicy.isUrgent(louder, weak))
    }

    func testSoftwareNewsIsShownBrieflyAndNeverInsteadOfAFreshBeep() {
        let installed = SoftwareNote.make(UpdateState(state: "installed", commit: "a46b01c9d8e7", seq: 4, detail: ""))
        XCTAssertEqual(installed?.software, .init(text: "Updated to a46b01c", problem: false))
        XCTAssertEqual(SoftwareNote.make(UpdateState(state: "rejected", commit: "a46b01c", seq: 4, detail: "x"))?.software.problem, true)
        XCTAssertEqual(SoftwareNote.make(UpdateState(state: "staged", commit: nil, seq: nil, detail: nil))?.software.problem, false)
        XCTAssertNil(SoftwareNote.make(UpdateState(state: "current", commit: "a46b01c", seq: 4, detail: "")))
        XCTAssertNil(SoftwareNote.make(UpdateState(state: "off", commit: nil, seq: nil, detail: nil)))
        XCTAssertNil(SoftwareNote.make(nil))

        var i = input()
        i.software = installed?.software
        XCTAssertEqual(LiveTileBuilder.make(i).software?.text, "Updated to a46b01c")
        i.lastAlert = alert(.chime, secondsAgo: 20)
        XCTAssertNil(LiveTileBuilder.make(i).software, "a beep 20 s ago keeps its line")
        i.lastAlert = alert(.chime, secondsAgo: 90)
        XCTAssertNotNil(LiveTileBuilder.make(i).software)

        var plain = LiveTileBuilder.make(input())
        var withNews = plain
        withNews.software = installed?.software
        XCTAssertTrue(LiveTilePolicy.isUrgent(plain, withNews), "news is sent at once")
        plain.software = installed?.software
        XCTAssertFalse(LiveTilePolicy.isUrgent(plain, withNews))
    }

    func testAnOlderPublisherWithoutUpdateStateStillDecodes() throws {
        let json = #"{"snapshot_age_s":0.1,"detector":{"state":"running"},"channels":[],"matches":[]}"#
        XCTAssertNil(try JSONDecoder().decode(PiState.self, from: Data(json.utf8)).update)
        let newer = #"{"snapshot_age_s":0.1,"detector":{"state":"running"},"channels":[],"matches":[],"update":{"state":"installed","commit":"a46b01c","seq":4,"detail":""}}"#
        XCTAssertEqual(try JSONDecoder().decode(PiState.self, from: Data(newer.utf8)).update,
                       UpdateState(state: "installed", commit: "a46b01c", seq: 4, detail: ""))
    }

    func testThePisOwnQuietLimitIsUsedWhenItSendsOne() throws {
        var settings = AlertSettings()
        settings.receiverQuietWindows = 40
        let old = try JSONDecoder().decode(DetectorState.self, from: Data(#"{"state":"running","quiet_windows":50}"#.utf8))
        XCTAssertEqual(StatusBuilder.quietLimit(old, settings), 40, "an older detector: the setting")
        let short = try JSONDecoder().decode(DetectorState.self, from: Data(#"{"state":"running","quiet_windows":50,"quiet_alarm_windows":120}"#.utf8))
        XCTAssertEqual(StatusBuilder.quietLimit(short, settings), 120, "0.5 s windows: 120 of them make a minute")
        XCTAssertEqual(StatusBuilder.quietLimit(nil, settings), 40)
    }

    func testTheNoiseFloorIsShownAgainstTheReceiverAlone() throws {
        XCTAssertEqual(Format.floor(85.2, quiet: 81.7), "85.2 dB, 3.5 above the receiver alone")
        XCTAssertEqual(Format.floor(81.0, quiet: 81.7), "81.0 dB, 0.0 above the receiver alone")
        XCTAssertEqual(Format.floor(85.2, quiet: nil), "85.2 dB")
        let detector = try JSONDecoder().decode(DetectorState.self, from: Data(#"{"state":"running","floor_db":85.2,"whole_band_windows":7,"windows":300}"#.utf8))
        XCTAssertEqual(detector.floorDB, 85.2)
        XCTAssertEqual(detector.wholeBandWindows, 7)
        var display = RemoteConfig.Display()
        display.floorQuietDB = 81.7
        XCTAssertTrue(display.problems.isEmpty)
        var style = DisplayStyle()
        XCTAssertNil(style.floorQuiet)
        style.apply(display)
        XCTAssertEqual(style.floorQuiet, 81.7)
    }

    func testInterferenceIsCountedOverAboutTheLastMinute() {
        var meter = InterferenceMeter()
        XCTAssertNil(meter.percent)
        meter.add(at: 0, windows: 100, wide: 10)
        meter.add(at: 5, windows: 110, wide: 12)
        XCTAssertNil(meter.percent, "too few windows yet")
        meter.add(at: 30, windows: 160, wide: 16)
        XCTAssertEqual(meter.percent ?? -1, 10, accuracy: 0.01)
        meter.add(at: 70, windows: 240, wide: 16)
        meter.add(at: 100, windows: 300, wide: 16)
        XCTAssertEqual(meter.percent ?? -1, 0, accuracy: 0.01, "the bursts of more than a minute ago no longer count")
        meter.add(at: 105, windows: 4, wide: 0)
        XCTAssertNil(meter.percent, "a new session starts again")
    }

    func testTheMostImportantProblemWins() {
        XCTAssertEqual(LiveTileBuilder.make(input(linkLost: true, running: false, quiet: true, db: 55)).phase, .linkLost)
        XCTAssertEqual(LiveTileBuilder.make(input(running: false, quiet: true, db: 55)).phase, .stopped)
        XCTAssertEqual(LiveTileBuilder.make(input(quiet: true, db: 55)).phase, .receiverQuiet)
        XCTAssertEqual(LiveTileBuilder.make(input()).phase, .listening)
        XCTAssertEqual(LiveTileBuilder.make(input(db: 30)).phase, .signal)
    }

    func testToneAndLevelMeanWhatTheMetersMean() {
        // Written for a chime from 22 dB; the default is 35 since 27 November 2026, as the server's.
        var from22 = AlertSettings()
        from22.red = 22
        let cases: [(Double, LiveTileState.Tone, Int)] = [(55, .strong, 10), (30, .chime, 6), (15, .silent, 3), (9, .weak, 1)]
        XCTAssertEqual(LiveTileBuilder.make(input(db: 30)).tone, .silent, "30 dB is below the default 35")
        for (db, tone, level) in cases {
            let s = LiveTileBuilder.make(input(db: db, settings: from22))
            XCTAssertEqual(s.tone, tone, "\(db) dB")
            XCTAssertEqual(s.level, level, "\(db) dB")
            XCTAssertEqual(s.strength, Int(db))
            XCTAssertEqual(s.channel, 154)
        }
        var quiet = AlertSettings()
        quiet.quiet = true
        XCTAssertEqual(LiveTileBuilder.make(input(db: 25, settings: quiet)).tone, .silent, "Quiet mode: 25 dB does not chime")
    }

    func testTheSameBurstKeepsTheSameTimeSoNothingIsResent() {
        let first = LiveTileBuilder.make(input(db: 30, age: 2.2))
        let later = LiveTileBuilder.make(input(db: 30, age: 5.2, at: now.addingTimeInterval(3)))
        XCTAssertEqual(first.heardAt, later.heardAt)
        XCTAssertEqual(first, later, "time passing alone changes nothing; the tile's clock counts by itself")
    }

    func testClockJitterAroundASecondBoundaryKeepsTheShownTime() {
        var i = input(db: 30, age: 2)
        i.topHeardAt = now.addingTimeInterval(-2.49)
        let first = LiveTileBuilder.make(i)
        i.topHeardAt = now.addingTimeInterval(-2.51)
        i.previous = first
        XCTAssertEqual(LiveTileBuilder.make(i).heardAt, first.heardAt, "the same burst, a hair later: no update")
        i.topHeardAt = now.addingTimeInterval(-0.2)
        XCTAssertNotEqual(LiveTileBuilder.make(i).heardAt, first.heardAt, "a new burst seconds later does move it")
    }

    func testANewBurstASecondLaterGetsItsOwnTime() {
        var i = input(db: 30, age: 2)
        i.topHeardAt = now.addingTimeInterval(-1)
        i.topBurstID = "s/w1-ch154"
        let first = LiveTileBuilder.make(i)
        i.previous = first
        i.topHeardAt = now.addingTimeInterval(-1.4)
        XCTAssertEqual(LiveTileBuilder.make(i).heardAt, first.heardAt, "same burst id: jitter ignored")
        i.topHeardAt = now.addingTimeInterval(0.2)
        i.topBurstID = "s/w2-ch154"
        XCTAssertNotEqual(LiveTileBuilder.make(i).heardAt, first.heardAt, "another burst id: its own time")
    }

    func testAMarkShowsBrieflyAndRecordingRequestsAreNotPassedIn() {
        func sighting(_ secondsAgo: Double) -> Sighting {
            Sighting(id: "s", label: .nothing, note: "", seenAt: now.addingTimeInterval(-secondsAgo), phase: .sending,
                     detail: "", isRecordingRequest: false, place: nil)
        }
        var i = input()
        i.lastMark = sighting(5)
        XCTAssertEqual(LiveTileBuilder.make(i).mark?.title, "Saw nothing")
        i.lastMark = sighting(31)
        XCTAssertNil(LiveTileBuilder.make(i).mark)
    }

    func testOnlyRecentSoundsAreShownAsTheLastBeep() {
        XCTAssertEqual(LiveTileBuilder.make(input(alert: alert(.urgent, secondsAgo: 30))).lastAlert?.urgent, true)
        XCTAssertEqual(LiveTileBuilder.make(input(alert: alert(.urgent, secondsAgo: 30))).lastAlert?.strength, 55)
        XCTAssertEqual(LiveTileBuilder.make(input(alert: alert(.chime, secondsAgo: 30))).lastAlert?.urgent, false)
        XCTAssertNil(LiveTileBuilder.make(input(alert: alert(.urgent, secondsAgo: 16 * 60))).lastAlert)
        XCTAssertNil(LiveTileBuilder.make(input(alert: alert(nil, secondsAgo: 5))).lastAlert, "a silent match is no beep")
        XCTAssertNil(LiveTileBuilder.make(input(alert: alert(.warning, secondsAgo: 5))).lastAlert)
    }

    func testTheStateStaysFarBelowTheFourKilobyteLimit() throws {
        var s = LiveTileBuilder.make(input(db: 55, frequent: true, alert: alert(.urgent, secondsAgo: 3)))
        s.badge = String(repeating: "W", count: 10)
        let size = try JSONEncoder().encode(s).count + JSONEncoder().encode(SlotActivityAttributes(startedAt: now)).count
        XCTAssertLessThan(size, 1024, "Apple allows 4 KB for attributes and state together")
    }
}

final class LiveTileIconTests: XCTestCase {
    /// A misspelt or too-new symbol draws nothing and raises no error, so check each one exists.
    func testEveryIconExistsOnTheOldestSupportedSystem() {
        var state = LiveTileState.placeholder
        var names = ["clock", "bell.fill", "waveform", "clock.badge.exclamationmark"]
        for phase in [LiveTileState.Phase.listening, .linkLost, .stopped, .receiverQuiet, .ended] {
            state.phase = phase
            names.append(state.actionSymbol)
        }
        state.phase = .signal
        for tone in [LiveTileState.Tone.none, .weak, .silent, .chime, .strong] {
            state.tone = tone
            names.append(state.actionSymbol)
        }
        for name in names {
            XCTAssertNotNil(UIImage(systemName: name), name)
        }
    }

    func testEachActionHasItsOwnIcon() {
        var state = LiveTileState.placeholder
        state.phase = .signal
        let icons = [LiveTileState.Tone.weak, .silent, .chime, .strong].map { tone -> String in
            state.tone = tone
            return state.actionSymbol
        }
        XCTAssertEqual(Set(icons).count, 4)
    }
}

final class LiveTilePolicyTests: XCTestCase {
    private func state(_ phase: LiveTileState.Phase = .signal, db: Int? = 30, tone: LiveTileState.Tone = .chime,
                       alertAt: Double? = nil) -> LiveTileState {
        var s = LiveTileState.placeholder
        s.phase = phase
        s.channel = 102
        s.strength = db
        s.tone = tone
        s.lastAlert = alertAt.map { .init(urgent: false, channel: 102, strength: 30, at: Date(timeIntervalSince1970: $0)) }
        return s
    }

    func testTheFirstStateIsSentAtOnce() {
        XCTAssertTrue(LiveTilePolicy().shouldSend(state(), now: 0))
    }

    func testNothingNewMeansOnlyAKeepAlive() {
        var p = LiveTilePolicy()
        p.didSend(state(), now: 100)
        XCTAssertFalse(p.shouldSend(state(), now: 101))
        XCTAssertFalse(p.shouldSend(state(), now: 119))
        XCTAssertTrue(p.shouldSend(state(), now: 120), "moves the stale date on")
    }

    func testOrdinaryChangesWaitTenSeconds() {
        var p = LiveTilePolicy()
        p.didSend(state(db: 30), now: 100)
        XCTAssertFalse(p.shouldSend(state(db: 31), now: 109.9))
        XCTAssertTrue(p.shouldSend(state(db: 31), now: 110))
    }

    func testAnotherChannelIsAnOrdinaryChange() {
        var p = LiveTilePolicy()
        p.didSend(state(), now: 100)
        var other = state()
        other.channel = 60
        XCTAssertFalse(p.shouldSend(other, now: 101), "no beep came with it, so it can wait")
        XCTAssertTrue(p.shouldSend(other, now: 110))
    }

    func testTheServerCanChangeTheSpacing() {
        var p = LiveTilePolicy()
        p.minInterval = 2
        p.didSend(state(db: 30), now: 100)
        XCTAssertTrue(p.shouldSend(state(db: 31), now: 102))
    }

    func testANewSightingGoesAtOnceButItsExpiryWaits() {
        var p = LiveTilePolicy()
        var marked = state()
        marked.mark = .init(title: "Police", at: Date(timeIntervalSince1970: 1_800_000_000))
        p.didSend(state(), now: 100)
        XCTAssertTrue(p.shouldSend(marked, now: 100.1))
        p.didSend(marked, now: 100.1)
        XCTAssertFalse(p.shouldSend(state(), now: 101))
    }

    func testTheUpdateNumberIsNotAChange() {
        XCTAssertNil(state().rev, "the builder and policy never set it; only sending does")
    }

    func testWhatMattersGoesAtOnce() {
        var p = LiveTilePolicy()
        p.didSend(state(), now: 100)
        XCTAssertTrue(p.shouldSend(state(alertAt: 1_800_000_000), now: 100.1), "a new beep")
        XCTAssertTrue(p.shouldSend(state(.linkLost), now: 100.1), "the link is lost")
        XCTAssertTrue(p.shouldSend(state(db: 55, tone: .strong), now: 100.1), "STRONG")
    }

    func testTheTileSaysNotUpdatingBeforeItCouldMislead() {
        XCTAssertGreaterThan(LiveTilePolicy.staleAfter, LiveTilePolicy.keepAlive * 2,
                             "two missed keep-alives before the tile calls itself stale")
        XCTAssertLessThanOrEqual(LiveTilePolicy.staleAfter, 60)
    }
}
