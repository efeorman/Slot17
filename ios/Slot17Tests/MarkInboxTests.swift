import XCTest

final class MarkInboxTests: XCTestCase {
    private var inbox: MarkInbox!

    override func setUp() {
        inbox = MarkInbox(url: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".json"))
    }

    override func tearDown() { inbox.clear() }

    func testATapIsKeptWithItsOwnTimeAndPlace() {
        let at = Date(timeIntervalSince1970: 1_800_000_000)
        let place = Place(lat: 52.1, lon: 5.1, speedKmh: 77, accuracyM: 10)
        inbox.add(PendingMark(label: .police, seenAt: at, place: place))
        inbox.add(PendingMark(label: .nothing, seenAt: at.addingTimeInterval(9), place: nil))
        XCTAssertEqual(inbox.all, [PendingMark(label: .police, seenAt: at, place: place),
                                   PendingMark(label: .nothing, seenAt: at.addingTimeInterval(9), place: nil)])
    }

    func testAnEmptyOrUnreadableInboxIsJustEmpty() throws {
        XCTAssertEqual(inbox.all, [])
        try Data("not json".utf8).write(to: inbox.url)
        XCTAssertEqual(inbox.all, [])
    }

    func testClearingEmptiesIt() {
        inbox.add(PendingMark(label: .police, seenAt: Date(), place: nil))
        inbox.clear()
        XCTAssertEqual(inbox.all, [])
    }

    func testItCannotGrowWithoutLimit() {
        for i in 0..<205 { inbox.add(PendingMark(label: .police, seenAt: Date(timeIntervalSince1970: Double(i)), place: nil)) }
        XCTAssertEqual(inbox.all.count, 200)
        XCTAssertEqual(inbox.all.last?.seenAt, Date(timeIntervalSince1970: 204), "the newest are kept")
    }

    func testADoubleTapIsOneSighting() {
        let at = Date(timeIntervalSince1970: 1_800_000_000)
        func saved(_ label: Sighting.Label, _ offset: TimeInterval, recording: Bool = false) -> Sighting {
            Sighting(id: Sighting.newID(), label: label, note: "", seenAt: at.addingTimeInterval(offset), phase: .saved,
                     detail: "", isRecordingRequest: recording, place: nil)
        }
        let police = [saved(.police, 0)]
        XCTAssertTrue(Sighting.isRepeat(.police, at: at.addingTimeInterval(1.5), among: police))
        XCTAssertTrue(Sighting.isRepeat(.police, at: at.addingTimeInterval(-2.9), among: police))
        XCTAssertFalse(Sighting.isRepeat(.police, at: at.addingTimeInterval(3), among: police), "a second car is a second sighting")
        XCTAssertFalse(Sighting.isRepeat(.ambulance, at: at.addingTimeInterval(1), among: police), "another button is another sighting")
        XCTAssertFalse(Sighting.isRepeat(.unknown, at: at.addingTimeInterval(1), among: [saved(.unknown, 0, recording: true)]),
                       "a recording request is not a sighting")
        XCTAssertFalse(Sighting.isRepeat(.police, at: at, among: []))
    }

    func testTheButtonsKindsAreRealLabels() {
        XCTAssertEqual(Sighting.Label(rawValue: "police"), .police)
        XCTAssertEqual(Sighting.Label(rawValue: "noise"), .nothing)
    }

    func testTheMarkSoundIsValidAndShort() {
        let spec = SoundKind.marked.builtIn
        XCTAssertNil(spec.problem)
        XCTAssertLessThan(spec.totalSeconds, 0.4, "a blip, not an alert")
        XCTAssertNotNil(SoundSynth.render(spec).map(abs).max())
    }

    func testTileGapIsCheckedAndApplied() {
        var display = RemoteConfig.Display()
        display.tileGapS = 0.2
        XCTAssertFalse(display.problems.isEmpty)
        display.tileGapS = 5
        XCTAssertTrue(display.problems.isEmpty)
        var style = DisplayStyle()
        XCTAssertEqual(style.tileGap, 10)
        style.apply(display)
        XCTAssertEqual(style.tileGap, 5)
    }
}
