import XCTest

final class RemoteConfigTests: XCTestCase {
    private func decode(_ json: String) -> RemoteConfig? {
        RemoteConfig.decode(Data(#"{"version":"abc123def456","config":"#.utf8) + Data(json.utf8) + Data("}".utf8))?.config
    }

    func testGoingBackToBuiltInRulesKeepsTheOperatorsOwnChanges() throws {
        var s = AlertSettings()
        let config = try XCTUnwrap(decode(#"{"rules":{"red_db":35,"urgent_db":60,"max_age_s":20}}"#))
        s.overrides = ["urgentLevel"]
        s.urgentLevel = 45
        s.apply(config)
        XCTAssertEqual(s.red, 35)
        XCTAssertEqual(s.maxAge, 20)
        s.restoreBuiltInRules()
        XCTAssertEqual(s.red, AlertSettings().red)
        XCTAssertEqual(s.maxAge, AlertSettings().maxAge)
        XCTAssertEqual(s.urgentLevel, 45)
    }

    func testTheShippedServerFileDecodesAndIsValid() throws {
        // scripts/e2e-app-config.json is the end-to-end test's own settings file. Its values are for the test;
        // display/app-config.json is what is deployed, and may differ.
        let ios = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let url = ios.appendingPathComponent("scripts/e2e-app-config.json")
        let data = try XCTUnwrap(try? Data(contentsOf: url), "scripts/e2e-app-config.json is missing")
        let config = try XCTUnwrap(decode(String(decoding: data, as: UTF8.self)))
        XCTAssertEqual(config.problems, [])
        XCTAssertEqual(config.rules?.redDB, 22)
    }

    func testAnEmptyFileChangesNothing() throws {
        let config = try XCTUnwrap(decode("{}"))
        XCTAssertEqual(config.problems, [])
        var s = AlertSettings()
        s.apply(config)
        XCTAssertEqual(s, AlertSettings())
    }

    func testOutOfRangeValuesAreRefused() throws {
        for bad in [#"{"rules":{"red_db":200}}"#, #"{"rules":{"red_db":20,"yellow_db":30}}"#,
                    #"{"rules":{"max_age_s":0}}"#, #"{"schema":2}"#, #"{"warnings":{"receiver_quiet_windows":0}}"#,
                    #"{"sounds":{"siren":{"notes":[[1000,0,0.1]]}}}"#,
                    #"{"sounds":{"urgent":{"wave":"sawtooth","notes":[[1000,0,0.1]]}}}"#,
                    #"{"sounds":{"urgent":{"notes":[[20,0,0.1]]}}}"#,
                    #"{"sounds":{"urgent":{"notes":[[1000,0,5]]}}}"#,
                    #"{"sounds":{"urgent":{"wav":"../secret.wav"}}}"#] {
            let config = try XCTUnwrap(decode(bad), bad)
            XCTAssertFalse(config.problems.isEmpty, bad)
        }
    }

    func testASoundCanBeJustAWavFile() throws {
        let config = try XCTUnwrap(decode(#"{"sounds":{"chime":{"wav":"soft_bell.wav"}}}"#))
        XCTAssertEqual(config.problems, [])
        XCTAssertEqual(config.sounds?["chime"]?.wav, "soft_bell.wav")
    }

    func testRulesApplyButTheOperatorsOwnChangesStay() throws {
        let config = try XCTUnwrap(decode(#"{"rules":{"red_db":30,"yellow_db":15,"urgent_db":55,"quiet_gap_s":45}}"#))
        var s = AlertSettings()
        s.red = 26
        s.overrides = ["red"]
        s.apply(config)
        XCTAssertEqual(s.red, 26, "set by hand, so kept")
        XCTAssertEqual(s.yellow, 15)
        XCTAssertEqual(s.urgentLevel, 55)
        XCTAssertEqual(s.quietGap, 45)
    }

    func testOnlyRuleChangesMadeByHandAreRemembered() {
        var b = AlertSettings()
        b.red = 30
        b.volume = 0.5
        XCTAssertEqual(AlertSettings.changedKeys(AlertSettings(), b), ["red"])
    }

    func testSettingsSavedByTheOldVersionStillLoad() throws {
        let old = #"{"red":25,"yellow":12,"sound":true,"urgent":true,"repeatNeeded":false,"antennaWarning":true,"linkLostTone":true,"volume":0.8,"beepStyle":"Over music","keepScreenOn":true,"autoSleepMinutes":30}"#
        let s = try JSONDecoder().decode(AlertSettings.self, from: Data(old.utf8))
        XCTAssertEqual(s.red, 25)
        XCTAssertEqual(s.volume, 0.8)
        XCTAssertEqual(s.urgentLevel, 50, "new fields keep their defaults")
        XCTAssertTrue(s.recordLocation)
    }
}

final class DisplayStyleTests: XCTestCase {
    private func decode(_ json: String) -> RemoteConfig? {
        RemoteConfig.decode(Data(#"{"version":"abc123def456","config":"#.utf8) + Data(json.utf8) + Data("}".utf8))?.config
    }

    func testTheBuiltInColoursPassTheirOwnChecks() {
        let d = DisplayStyle()
        let display = RemoteConfig.Display(weakColour: d.weak, silentColour: d.silent, chimeColour: d.chime,
                                           strongOutlineColour: d.strongOutline, strongBadge: d.strongBadge,
                                           frequentText: d.frequentText, pulseOnNewStrong: true)
        XCTAssertEqual(display.problems, [])
    }

    func testColoursAndWordsComeFromTheServer() throws {
        let config = try XCTUnwrap(decode(##"{"display":{"weak_colour":"#8899AA","strong_badge":"STERK","frequent_text":"Veel bursts","pulse_on_new_strong":false}}"##))
        XCTAssertEqual(config.problems, [])
        var style = DisplayStyle()
        style.apply(config.display)
        XCTAssertEqual(style.weak, "#8899AA")
        XCTAssertEqual(style.strongBadge, "STERK")
        XCTAssertEqual(style.frequentText, "Veel bursts")
        XCTAssertFalse(style.pulseOnNewStrong)
        XCTAssertEqual(style.chime, DisplayStyle().chime, "missing keys keep the built-in value")
        style.apply(nil)
        XCTAssertEqual(style, DisplayStyle(), "a file without display settings goes back to built-in")
    }

    func testUnreadableOrMisleadingColoursAreRefused() throws {
        for bad in [##"{"display":{"chime_colour":"red"}}"##, ##"{"display":{"weak_colour":"#333333"}}"##,
                    ##"{"display":{"weak_colour":"#2FD26B"}}"##, ##"{"display":{"silent_colour":"#FF4D4F","chime_colour":"#ff4d4f"}}"##,
                    ##"{"display":{"strong_badge":""}}"##, ##"{"display":{"strong_badge":"VERY STRONG SIGNAL"}}"##] {
            let config = try XCTUnwrap(decode(bad), bad)
            XCTAssertFalse(config.problems.isEmpty, bad)
        }
    }
}

final class SoundSynthTests: XCTestCase {
    func testEveryBuiltInSoundIsValidAndAudible() {
        for kind in [SoundKind.urgent, .chime, .warning, .linkLost] {
            let spec = kind.builtIn
            XCTAssertNil(spec.problem, "\(kind)")
            let samples = SoundSynth.render(spec)
            XCTAssertEqual(Double(samples.map(abs).max() ?? 0), spec.peak, accuracy: 0.001, "\(kind)")
            XCTAssertEqual(Double(samples.count) / 44_100, spec.totalSeconds + 0.02, accuracy: 0.001, "\(kind)")
        }
    }

    func testTheUrgentBeepKeepsItsThreePulses() {
        let samples = SoundSynth.render(SoundKind.urgent.builtIn)
        let loud = samples.map { abs($0) > 0.3 }
        var starts = 0
        for i in 1..<loud.count where loud[i] && !loud[i - 1] && (i < 200 || !loud[(i - 200)..<i].contains(true)) { starts += 1 }
        XCTAssertEqual(starts, 3)
    }

    func testABellRingsOut() {
        let spec = SoundSpec(wave: "bell", peak: 0.6, attackS: 0.008, releaseS: 0.05, fadeS: 0.45, notes: [[880, 0, 1.2]])
        XCTAssertNil(spec.problem)
        let samples = SoundSynth.render(spec)
        let early = samples[441..<4410].map(abs).max() ?? 0
        let late = samples[44100..<48510].map(abs).max() ?? 0
        XCTAssertGreaterThan(early, late * 5, "a bell fades")
    }
}

final class QuietModeTests: XCTestCase {
    private var played: [SoundKind] = []

    private func run(_ settings: AlertSettings, _ db: Double, id: String, now: Double, engine: AlertEngine) -> AlertDecision? {
        engine.ingest([MatchRow(id: id, ch: 1, strength: db, age: 1)], frame: nil, channels: [], scope: "s")
        return engine.flush(AlertEngine.Context(
            now: now, live: true, mayHold: false, scope: "s", settings: settings, armed: true, audioProblem: nil,
            mutedUntil: -1, age: { $0.match.age }, play: { [weak self] kind in self?.played.append(kind); return nil })).first
    }

    func testQuietModeRaisesTheChimeLevelButNeverTouchesUrgent() {
        var s = AlertSettings()
        s.red = 22          // Quiet mode's 28 counts when it is higher than the chime level (35 by default)
        s.quiet = true
        let engine = AlertEngine()
        XCTAssertEqual(run(s, 25, id: "a", now: 100, engine: engine)?.outcome,
                       .silent("25.0 dB is below the 28 dB red level (Quiet mode)"))
        _ = run(s, 55, id: "b", now: 101, engine: engine)
        XCTAssertEqual(played, [.urgent])
    }

    func testQuietModeSpacesChimesOut() {
        var s = AlertSettings()
        s.quiet = true
        let engine = AlertEngine()
        _ = run(s, 35, id: "a", now: 100, engine: engine)
        XCTAssertEqual(run(s, 35, id: "b", now: 120, engine: engine)?.outcome, .silent("this channel chimed under 30 s ago"))
        XCTAssertEqual(played, [.chime])
    }

    func testARedLevelAboveTheUrgentLevelNeverSilencesUrgentBeeps() {
        var s = AlertSettings()
        s.red = 60
        _ = run(s, 55, id: "a", now: 100, engine: AlertEngine())
        XCTAssertEqual(played, [.urgent])
    }

    func testServerRulesReachTheEngine() {
        var s = AlertSettings()
        s.urgentLevel = 45
        s.anyGap = 20
        let engine = AlertEngine()
        _ = run(s, 46, id: "a", now: 100, engine: engine)
        XCTAssertEqual(played, [.urgent])
    }
}

final class MarkSummaryTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_800_000_000)

    private func alert(_ seconds: Double, _ sound: SoundKind?) -> AlertRecord {
        AlertRecord(id: "\(seconds)", at: t0.addingTimeInterval(seconds), channel: 1, strength: 30, age: 1, sound: sound, reason: "")
    }

    private func mark(_ seconds: Double, _ label: Sighting.Label) -> Sighting {
        Sighting(id: Sighting.newID(), label: label, note: "", seenAt: t0.addingTimeInterval(seconds),
                 phase: .saved, detail: "", isRecordingRequest: false)
    }

    func testTheSevenNovemberDriveShape() {
        // Urgent beeps then a police mark; chimes then saw-nothing marks; a silent match; a stationary
        // police car marked with a chime before it; and one police car with no sound at all.
        let alerts = [alert(0, .urgent), alert(6, .urgent), alert(300, .chime), alert(330, .chime),
                      alert(500, nil), alert(900, .chime)]
        let marks = [mark(17, .police), mark(340, .nothing), mark(920, .police), mark(2000, .police)]
        let s = MarkSummary.make(alerts: alerts, sightings: marks, window: 120)
        XCTAssertEqual(s.urgent, MarkSummary.Row(played: 2, vehicleSeen: 2, nothingSeen: 0))
        XCTAssertEqual(s.chime, MarkSummary.Row(played: 3, vehicleSeen: 1, nothingSeen: 2))
        XCTAssertEqual(s.vehiclesMarked, 3)
        XCTAssertEqual(s.vehiclesWithoutSound, 1)
    }

    func testRecordingRequestsAreNotSightings() {
        var request = mark(10, .unknown)
        request.isRecordingRequest = true
        let s = MarkSummary.make(alerts: [alert(0, .chime)], sightings: [request], window: 120)
        XCTAssertEqual(s.chime.unmarked, 1)
    }

    func testAPlaceGoesIntoTheNoteForThePi() {
        let place = Place(lat: 52.370216, lon: 4.895168, speedKmh: 98.4, accuracyM: 6)
        XCTAssertEqual(place.noteText, "lat=52.370216;lon=4.895168;kmh=98;acc_m=6")
    }
}
