import XCTest

final class FaultNoticesTests: XCTestCase {
    private func input(_ state: String?, _ reasons: [String] = [], quiet: Bool = false, expecting: Bool = false) -> FaultNotices.Input {
        .init(detectorState: state, reasons: reasons, receiverQuiet: quiet, expectingSilence: expecting)
    }

    func testOneNoticePerEpisodeAndOneWhenFine() {
        var n = FaultNotices()
        XCTAssertNil(n.step(input("running"), now: 0))
        XCTAssertEqual(n.step(input("degraded", ["dropped samples"]), now: 10)?.title, "Slot17: dropped samples")
        XCTAssertNil(n.step(input("degraded", ["dropped samples"]), now: 100), "no reminder")
        XCTAssertNil(n.step(input("degraded", ["dropped samples"]), now: 1000))
        XCTAssertEqual(n.step(input("running"), now: 1010)?.title, "Slot17: running normally again")
        XCTAssertNil(n.step(input("running"), now: 2000))
    }

    func testAtMostOneAMinuteAndNothingIsLost() {
        var n = FaultNotices()
        XCTAssertNotNil(n.step(input("degraded", ["a"]), now: 0))
        XCTAssertNil(n.step(input("degraded", ["a", "b"]), now: 30), "within the minute")
        XCTAssertEqual(n.step(input("degraded", ["a", "b"]), now: 61)?.title, "Slot17: b", "sent after the wait")
    }

    func testNoCarDataIsNotAFaultAndStartingWaits() {
        var n = FaultNotices()
        XCTAssertNotNil(n.step(input("degraded", ["a"]), now: 0))
        XCTAssertNil(n.step(input(nil), now: 100), "parked: nothing")
        XCTAssertNil(n.step(input("starting"), now: 200), "not fine yet")
        XCTAssertNotNil(n.step(input("running"), now: 300))
    }

    func testAStopFromTheAppIsNotAFault() {
        var n = FaultNotices()
        XCTAssertNil(n.step(input("stopped", expecting: true), now: 0))
        XCTAssertEqual(n.step(input("stopped"), now: 100)?.title, "Slot17: Detector stopped")
    }

    func testAFaultThatReturnsIsAnnouncedAgain() {
        var n = FaultNotices()
        XCTAssertNotNil(n.step(input("running", quiet: true), now: 0))
        XCTAssertNotNil(n.step(input("degraded", ["a"], quiet: true), now: 100))
        XCTAssertNil(n.step(input("degraded", ["a"]), now: 200), "quiet cleared, a remains")
        XCTAssertEqual(n.step(input("degraded", ["a"], quiet: true), now: 300)?.title, "Slot17: The receiver hears nothing")
    }
}
