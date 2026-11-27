import XCTest

class Base: XCTestCase {
    static let liveLink = "http://127.0.0.1:5732/?key=e2e-viewer-key-0123456789"
    var app: XCUIApplication!

    override func setUp() {
        continueAfterFailure = false
        XCUIDevice.shared.orientation = .portrait
        // Location access is granted before the tests run; this only catches a prompt if it appears anyway.
        addUIInterruptionMonitor(withDescription: "Location") { alert in
            for title in ["Allow While Using App", "Allow Once", "Allow"] where alert.buttons[title].exists {
                alert.buttons[title].tap()
                return true
            }
            return false
        }
    }

    /// Flips a switch by tapping its knob side; tapping a label does not toggle it.
    func flip(_ identifier: String) {
        let toggle = reveal(app.switches[identifier].firstMatch)
        toggle.coordinate(withNormalizedOffset: CGVector(dx: 0.93, dy: 0.5)).tap()
    }

    /// Twice on Codemagic the first launch of a test session ignored its launch arguments (8 November):
    /// once the app came up without --no-audio and hung, once without --fresh, showing the previous test's
    /// data. So each session throws its first launch away. The simulator-wide SLOT17_NO_AUDIO keeps it safe.
    private static var sessionWarmedUp = false

    func launch(_ arguments: [String]) {
        if !Self.warmedUpInThisProcess() {
            let first = XCUIApplication()
            first.launch()
            first.terminate()
        }
        app = XCUIApplication()
        // A Mac with no audio system (Codemagic): everything except the sound itself is still tested.
        let noAudio = ProcessInfo.processInfo.environment["NO_AUDIO"] == "1" && !arguments.contains("--no-audio")
        app.launchArguments = arguments + (noAudio ? ["--no-audio"] : [])
        print("LAUNCH \(app.launchArguments.filter { !$0.contains("key=") }), NO_AUDIO=\(ProcessInfo.processInfo.environment["NO_AUDIO"] ?? "unset")")
        app.launch()
    }

    private static func warmedUpInThisProcess() -> Bool {
        defer { Base.sessionWarmedUp = true }
        return Base.sessionWarmedUp
    }

    /// Saved next to the other results, so the screens themselves can be looked at.
    func shot(_ name: String) {
        guard let folder = ProcessInfo.processInfo.environment["SHOT_DIR"] else { return }
        let url = URL(fileURLWithPath: folder).appendingPathComponent(name + ".png")
        try? XCUIScreen.main.screenshot().pngRepresentation.write(to: url)
    }

    /// An element by its identifier, whatever type iOS reports it as.
    func any(_ identifier: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: identifier).firstMatch
    }

    func tab(_ name: String) {
        let button = app.buttons["tab-\(name)"]
        XCTAssertTrue(button.waitForExistence(timeout: 10), "tab \(name)")
        button.tap()
    }

    func waitFor(_ element: XCUIElement, label: String? = nil, contains: String? = nil, timeout: TimeInterval = 30,
                 file: StaticString = #filePath, line: UInt = #line) {
        var predicate = NSPredicate(format: "exists == true")
        if let label { predicate = NSPredicate(format: "label == %@", label) }
        if let contains { predicate = NSPredicate(format: "label CONTAINS %@", contains) }
        let done = expectation(for: predicate, evaluatedWith: element)
        let result = XCTWaiter().wait(for: [done], timeout: timeout)
        if result != .completed {
            // What was really on the screen, so one failed run says why without another run.
            shot("failure-\(line)")
            print("SCREEN AT FAILURE (line \(line), app state \(app.state.rawValue)):\n\(String(app.debugDescription.prefix(6000)))")
        }
        XCTAssertEqual(result, .completed, "waiting for \(label ?? contains ?? "element"), found: \(element.exists ? element.label : "nothing")", file: file, line: line)
    }

    /// Scrolls until the element can be tapped, for buttons below the fold.
    @discardableResult
    func reveal(_ element: XCUIElement, file: StaticString = #filePath, line: UInt = #line) -> XCUIElement {
        // Either direction: a screen keeps its scroll position. Asked again after a moment, because a swipe
        // keeps moving: an element can be tappable mid-scroll and gone a moment later.
        func ready() -> Bool {
            guard element.exists && element.isHittable else { return false }
            usleep(1_000_000)
            return element.exists && element.isHittable
        }
        var tries = 0
        while !ready() && tries < 12 {
            app.swipeUp(velocity: .slow)
            tries += 1
        }
        tries = 0
        while !ready() && tries < 16 {
            app.swipeDown(velocity: .slow)
            tries += 1
        }
        let found = ready()
        if !found {
            shot("failure-\(line)")
            print("SCREEN AT FAILURE (line \(line)):\n\(String(app.debugDescription.prefix(6000)))")
        }
        XCTAssertTrue(found, "could not reach \(element)", file: file, line: line)
        return element
    }
}

/// The quick run: the screens changed most often, without the long drive test.
final class QuickDesignTests: Base {
    func testTheLiveScreenAndTileDesigns() {
        launch(["--fresh", "--demo", "--screenshot-values", "--no-audio"])
        waitFor(app.staticTexts["headline"], label: "Demo running")
        sleep(1)
        shot("01-live-portrait")
        tab("Settings")
        reveal(app.buttons["live-activity-preview-button"]).tap()
        XCTAssertTrue(app.navigationBars["Live Activity"].waitForExistence(timeout: 5))
        sleep(1)
        shot("13-live-activity-carplay")
        app.swipeUp()
        sleep(1)
        shot("14-live-activity-lock-screen")
        app.navigationBars["Live Activity"].buttons["Done"].tap()
        waitFor(any("live-activity-status"), contains: "Showing", timeout: 20)
    }

    /// 1.6.1: every kind of custom button answers a tap away from its words, as a finger lands. App 1.6.0
    /// passed centre taps here, and its buttons did not answer on the operator's iPhone.
    func testButtonsAnswerAwayFromTheirWords() {
        launch(["--fresh", "--demo", "--screenshot-values", "--no-audio"])
        waitFor(app.staticTexts["headline"], label: "Demo running")
        func edgeTap(_ element: XCUIElement) {
            XCTAssertTrue(element.waitForExistence(timeout: 10), element.identifier)
            element.coordinate(withNormalizedOffset: CGVector(dx: 0.15, dy: 0.8)).tap()
        }
        // I SAW: a sighting is taken.
        edgeTap(reveal(app.buttons["mark-police"]))
        tab("Sightings")
        waitFor(any("sighting-status"), contains: "Demo only", timeout: 10)
        // Mute, a square glass button.
        tab("Live")
        let mute = app.buttons["mute"]
        edgeTap(mute)
        waitFor(mute, label: "Unmute", timeout: 5)
        edgeTap(mute)
        waitFor(mute, label: "Mute for 30 seconds", timeout: 5)
        // Settings: a wide button with the button style, opening a sheet each.
        tab("Settings")
        edgeTap(reveal(app.buttons["edit-link"]))
        XCTAssertTrue(app.navigationBars["Private link"].waitForExistence(timeout: 5), "the link sheet opens")
        app.navigationBars["Private link"].buttons["Cancel"].tap()
        edgeTap(reveal(app.buttons["live-activity-preview-button"]))
        XCTAssertTrue(app.navigationBars["Live Activity"].waitForExistence(timeout: 5), "the preview opens")
        shot("16-preview-opened-by-an-edge-tap")
        app.navigationBars["Live Activity"].buttons["Done"].tap()
    }

    /// 1.6.0: fault notifications from the app, and the car computer's Pushover switch (off in demo mode).
    func testTheFaultMessagesCard() {
        launch(["--fresh", "--demo", "--screenshot-values", "--no-audio"])
        waitFor(app.staticTexts["headline"], label: "Demo running")
        tab("Settings")
        // 1.8.0: Sound for aircraft, on by default, with the helicopter and its words above the volume.
        let aircraft = reveal(app.switches["setting-aircraft"].firstMatch)
        XCTAssertEqual(aircraft.value as? String, "1", "Sound for aircraft is on by default")
        reveal(app.sliders["Volume"].firstMatch)
        sleep(1)
        shot("15a-settings-aircraft")
        let pushover = reveal(app.switches["setting-pushover"])
        XCTAssertFalse(pushover.isEnabled, "the car's Pushover cannot be switched in demo mode")
        XCTAssertTrue(app.switches["setting-fault-notices"].exists)
        shot("15-settings-fault-messages")
    }
}

/// The widget's real button and its real intent, run in the app. CarPlay itself cannot be tested here.
final class WidgetMarkTests: Base {
    func testAWidgetTapBecomesASightingAndTheTileSaysSo() {
        launch(["--fresh", "--demo", "--screenshot-values", "--no-audio", "--real-widget-buttons"])
        waitFor(app.staticTexts["headline"], label: "Demo running")
        tab("Settings")
        reveal(app.buttons["live-activity-preview-button"]).tap()
        let button = app.buttons["widget-mark-noise"]
        XCTAssertTrue(button.waitForExistence(timeout: 5))
        shot("13a-widget")
        button.tap()
        sleep(1)
        app.navigationBars["Live Activity"].buttons["Done"].tap()
        tab("Sightings")
        waitFor(any("sighting-status"), contains: "Demo only", timeout: 10)
        shot("13b-widget-marked")
    }

    func testATapWhileTheAppWasClosedIsNotLost() {
        launch(["--fresh", "--demo", "--screenshot-values", "--no-audio", "--seed-pending-mark"])
        waitFor(app.staticTexts["headline"], label: "Demo running")
        tab("Sightings")
        waitFor(any("sighting-status"), contains: "Demo only", timeout: 10)
        XCTAssertEqual(app.descendants(matching: .any).matching(identifier: "sighting-status").count, 1)
        // Once only: a real restart must not add it again.
        app.terminate()
        launch(["--demo", "--screenshot-values", "--no-audio"])
        waitFor(app.staticTexts["headline"], label: "Demo running")
        tab("Sightings")
        waitFor(any("sighting-status"), timeout: 10)
        XCTAssertEqual(app.descendants(matching: .any).matching(identifier: "sighting-status").count, 1)
    }
}

/// A look at the phone's own widget list: does Slot17 appear there, on a clean simulator? Pictures and a
/// screen dump only, without passing or failing the run, since those screens change with each iOS.
final class WidgetListLook: Base {
    func testLookForSlot17InTheWidgetList() {
        launch(["--fresh", "--demo", "--no-audio"])
        sleep(3)
        XCUIDevice.shared.press(.home)
        let home = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        sleep(2)
        home.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.55)).press(forDuration: 2)
        sleep(1)
        shot("40-home-held")
        for label in ["Edit Home Screen", "Edit"] where home.buttons[label].exists { home.buttons[label].tap(); break }
        sleep(1)
        shot("41-home-edit")
        for label in ["Add Widget", "Add Widgets"] where home.buttons[label].exists { home.buttons[label].tap(); break }
        sleep(2)
        shot("42-widget-list")
        let search = home.searchFields.firstMatch
        if search.waitForExistence(timeout: 5) {
            search.tap()
            search.typeText("Slot17")
            sleep(3)
            shot("43-widget-search")
        }
        let dump = String(home.debugDescription.prefix(12000))
        print("WIDGET LIST DUMP, Slot17 found: \(dump.contains("Slot17"))\n\(dump)")
    }
}

/// Every screen, upright and sideways, on demo data. These pictures are for a person to judge.
final class ScreenshotTests: Base {
    func testEveryScreenInBothOrientations() {
        launch(["--fresh", "--demo", "--screenshot-values"])
        waitFor(app.staticTexts["headline"], label: "Demo running")
        XCTAssertTrue(app.staticTexts["DEMO. NOT REAL DETECTIONS"].exists, "demo data must always say so")
        sleep(1)
        shot("01-live-portrait")

        XCUIDevice.shared.orientation = .landscapeLeft
        sleep(3)
        shot("02-dashboard-landscape")
        XCTAssertGreaterThan(app.windows.firstMatch.frame.width, app.windows.firstMatch.frame.height)
        // A real tap where the button is, then the result, not just whether iOS calls it hittable.
        app.buttons["mark-police"].coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
        waitFor(any("last-sighting"), contains: "Demo only", timeout: 5)
        shot("02b-dashboard-marked")
        XCUIDevice.shared.orientation = .portrait
        sleep(2)

        // The button must work with iPhone's rotation lock on, where turning the phone would not.
        app.buttons["open-dashboard"].tap()
        sleep(3)
        XCTAssertGreaterThan(app.windows.firstMatch.frame.width, app.windows.firstMatch.frame.height, "dashboard button")
        app.buttons["close-dashboard"].tap()
        sleep(3)
        XCTAssertLessThan(app.windows.firstMatch.frame.width, app.windows.firstMatch.frame.height, "closing the dashboard")

        for (index, name) in ["Sightings", "Pi", "Logs", "Settings"].enumerated() {
            tab(name)
            sleep(1)
            shot("0\(index + 3)-\(name.lowercased())-portrait")
            XCUIDevice.shared.orientation = .landscapeLeft
            sleep(2)
            shot("0\(index + 3)-\(name.lowercased())-landscape")
            XCTAssertGreaterThan(app.windows.firstMatch.frame.width, app.windows.firstMatch.frame.height,
                                 "\(name) must turn sideways, also after the dashboard button was used")
            XCTAssertTrue(app.buttons["tab-Live"].exists, "\(name) sideways keeps the tabs")
            XCUIDevice.shared.orientation = .portrait
            sleep(2)
        }

        tab("Logs")
        app.buttons["Diagnostics"].tap()
        sleep(1)
        shot("07-logs-diagnostics")
        app.swipeUp()
        sleep(1)
        shot("08-logs-events")

        tab("Settings")
        app.swipeUp()
        sleep(1)
        shot("09-settings-lower")
        app.swipeUp()
        sleep(1)
        shot("10-settings-bottom")

        // The Live Activity's designs, drawn by the same views the widget extension uses.
        reveal(app.buttons["live-activity-preview-button"]).tap()
        XCTAssertTrue(app.navigationBars["Live Activity"].waitForExistence(timeout: 5))
        sleep(1)
        shot("13-live-activity-carplay")
        app.swipeUp()
        sleep(1)
        shot("14-live-activity-lock-screen")
        app.swipeUp()
        sleep(1)
        shot("15-live-activity-not-updating")
        app.navigationBars["Live Activity"].buttons["Done"].tap()
        sleep(1)
        waitFor(any("live-activity-status"), contains: "Showing", timeout: 20)
        shot("16-live-activity-status")
    }

    func testFirstRunAsksForTheLinkAndRefusesNonsense() {
        launch(["--fresh"])
        waitFor(app.staticTexts["headline"], label: "Not set up")
        shot("11-first-run")
        app.buttons["setup-link"].tap()
        let field = app.textFields["link-field"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        field.typeText("not a link")
        app.buttons["save-link"].tap()
        waitFor(app.staticTexts["link-problem"], contains: "not a private link")
        shot("12-link-refused")
        XCTAssertTrue(field.exists, "the sheet stays open")
    }

    func testThePrivateLinkSurvivesARestart() {
        launch(["--fresh", "--link", "http://192.0.2.1:9/?key=abcdefgh12345"])
        tab("Settings")
        waitFor(app.staticTexts["192.0.2.1:9"])
        app.terminate()
        launch([])
        tab("Settings")
        waitFor(app.staticTexts["192.0.2.1:9"])
    }
}

/// The real app against the real server and publisher. See scripts/e2e_harness.py.
final class EndToEndTests: Base {
    func testTheWholeDrive() {
        launch(["--fresh", "--link", Self.liveLink])
        let headline = app.staticTexts["headline"]

        // Connected, with the chain showing each link.
        waitFor(headline, label: "Detector running", timeout: 40)
        waitFor(any("chain-pi"), contains: "Connected")
        waitFor(any("chain-receiver"), contains: "No fault")
        shot("20-e2e-live")

        // Real matches arrive and are sorted into urgent, chime and silent.
        tab("Logs")
        waitFor(any("alert-row"), timeout: 30)
        sleep(10)
        shot("21-e2e-alerts")
        tab("Live")

        // A sighting travels to the Pi and comes back with what the detector did.
        app.buttons["mark-police"].tap()
        waitFor(any("last-sighting"), contains: "Saved with recording", timeout: 30)
        shot("22-e2e-sighting")
        tab("Sightings")
        waitFor(any("sighting-status"), contains: "Saved with recording")

        // The server's settings: its message shows on the Live screen, and the app says what it uses.
        tab("Live")
        waitFor(any("server-message"), contains: "Test settings", timeout: 30)

        // The urgent-level channel (55 dB) is marked STRONG, with the server's words, and its
        // dense bursts are labelled; the 15 and 30 dB meters are not marked strong.
        waitFor(any("meter-strong"), contains: "KRACHTIG", timeout: 30)
        waitFor(any("meter-strong"), contains: "Veel bursts", timeout: 30)
        XCTAssertEqual(app.descendants(matching: .any).matching(identifier: "meter-strong").count, 1)
        shot("22a-e2e-strong-meter")
        tab("Settings")
        reveal(any("meter-legend"))
        waitFor(any("meter-legend"), contains: "KRACHTIG", timeout: 10)
        shot("22a2-e2e-meter-legend")
        // The Live Activity started with the drive (in the simulator, without CarPlay).
        reveal(any("live-activity-status"))
        waitFor(any("live-activity-status"), contains: "Showing", timeout: 20)
        tab("Live")

        // Saw nothing, the new button, travels to the Pi like any sighting, with the phone's location.
        app.buttons["mark-noise"].tap()
        waitFor(any("last-sighting"), contains: "Saw nothing", timeout: 30)
        shot("22b-e2e-saw-nothing")

        // Quiet mode: on for a while (the server set its level to 35 dB, so 30 dB chimes go silent), then off.
        tab("Settings")
        flip("setting-quiet")
        XCTAssertEqual(app.switches["setting-quiet"].firstMatch.value as? String, "1", "Quiet mode switched on")
        tab("Live")
        waitFor(any("quiet-indicator"), timeout: 10)
        shot("22c-e2e-quiet-on")
        sleep(22)
        tab("Settings")
        flip("setting-quiet")
        reveal(any("config-status"))
        waitFor(any("config-status"), contains: "Server, version")
        shot("22d-e2e-server-settings")
        app.swipeDown(velocity: .fast)
        app.swipeDown(velocity: .fast)
        app.swipeDown(velocity: .fast)
        tab("Sightings")
        waitFor(any("mark-summary"), timeout: 10)
        shot("22e-e2e-mark-summary")

        // A sighting with a note, from the form.
        app.buttons["add-sighting"].tap()
        let note = app.textViews["sighting-note"].exists ? app.textViews["sighting-note"] : app.textFields["sighting-note"]
        XCTAssertTrue(note.waitForExistence(timeout: 5))
        note.tap()
        note.typeText("blue lights")
        app.buttons["save-sighting"].tap()
        sleep(2)

        // Save a recording, then the Pi's logs, from the Pi screen.
        tab("Pi")
        reveal(app.buttons["save-recording"]).tap()
        shot("23-e2e-pi")
        let collect = reveal(app.buttons["collect-pi-logs"])
        collect.tap()
        waitFor(any("status-logs"), contains: "Saved as pi-", timeout: 40)

        // The app's own log goes to the server, and both can be read back.
        tab("Logs")
        app.buttons["Diagnostics"].tap()
        reveal(app.buttons["send-drive-data"]).tap()
        waitFor(any("drive-upload-status"), contains: "Sent as app-", timeout: 30)
        reveal(app.buttons["send-app-log"]).tap()
        waitFor(any("log-upload-status"), contains: "Saved as app-", timeout: 30)
        shot("24-e2e-log-sent")
        app.swipeDown(velocity: .fast)
        app.swipeDown(velocity: .fast)
        app.buttons["On server"].tap()
        let row = any("server-log-row")
        XCTAssertTrue(row.waitForExistence(timeout: 15), "stored logs are listed")
        shot("25-e2e-server-logs")
        row.tap()
        waitFor(any("server-log-text"), contains: "App version", timeout: 20)
        shot("26-e2e-log-text")
        app.navigationBars.buttons.firstMatch.tap()

        // With the app in the background, beeps must keep coming. A simulator does not enforce
        // iPhone's background rules, so this tests the logic, not whether iPhone allows it.
        XCUIDevice.shared.press(.home)
        sleep(20)
        app.activate()
        tab("Live")
        waitFor(headline, label: "Detector running", timeout: 20)

        // The harness cuts the car off when it sees a Fire sighting: the phone must say so, and
        // warn once, then recover by itself.
        app.buttons["mark-fire"].tap()
        waitFor(headline, label: "No data from the car", timeout: 30)
        waitFor(any("chain-pi"), contains: "Silent")
        shot("27-e2e-car-lost")
        waitFor(headline, label: "Detector running", timeout: 45)

        // Stop reaches the Pi, the phone shows it, and the harness then restarts as a power-up would.
        tab("Pi")
        let stop = reveal(app.buttons["stop-detector"])
        stop.tap()
        confirm("Stop the detector")
        tab("Live")
        waitFor(headline, label: "Detector stopped", timeout: 30)
        shot("28-e2e-stopped")
        waitFor(headline, label: "Detector running", timeout: 45)

        // Power off needs a confirmation: cancelling must send nothing.
        tab("Pi")
        let power = reveal(app.buttons["power-off"])
        power.tap()
        shot("29-e2e-power-off-confirm")
        confirm("Power off")
        waitFor(any("status-poweroff"), contains: "Powering off", timeout: 30)
        shot("30-e2e-powering-off")
        tab("Live")
        waitFor(headline, label: "No data from the car", timeout: 30)
    }

    /// The dialog's button, not the one that opened it, which may have the same words.
    private func confirm(_ title: String, file: StaticString = #filePath, line: UInt = #line) {
        let matching = app.buttons.matching(NSPredicate(format: "label == %@", title))
        let deadline = Date().addingTimeInterval(5)
        while matching.count == 0 && Date() < deadline { usleep(200_000) }
        let last = matching.allElementsBoundByIndex.last { $0.isHittable }
        XCTAssertNotNil(last, "dialog button \(title)", file: file, line: line)
        last?.tap()
    }
}
