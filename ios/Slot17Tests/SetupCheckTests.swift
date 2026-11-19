import XCTest

/// The Pi screen's setup check: the same cases as android/core SetupCheckTest.kt where they share rules.
final class SetupCheckTests: XCTestCase {
    private let good = [
        ChainItem(id: "server", name: "Server", level: .ok, detail: "Connected"),
        ChainItem(id: "pi", name: "Pi", level: .ok, detail: "Connected"),
        ChainItem(id: "detector", name: "Detector", level: .ok, detail: "Running"),
        ChainItem(id: "receiver", name: "Receiver", level: .ok, detail: "No fault"),
    ]

    func testNoLinkIsTheOnlyThingSaid() {
        let rows = SetupCheck.phone(hasLink: false, chain: good, notifications: "Allowed", armed: true, sound: true)
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows[0].level, .bad)
        XCTAssertTrue(rows[0].fix?.contains("/server create") == true)
    }

    func testAllGood() {
        let rows = SetupCheck.phone(hasLink: true, chain: good, notifications: "Allowed", armed: true, sound: true)
        XCTAssertTrue(rows.allSatisfy { $0.level == .ok })
        XCTAssertNil(SetupCheck.first(rows))
    }

    func testABrokenLinkGetsItsFixAndComesFirst() {
        var chain = good
        chain[1].level = .bad
        chain[1].detail = "Not heard"
        let rows = SetupCheck.phone(hasLink: true, chain: chain, notifications: "Allowed", armed: false, sound: true)
        let first = SetupCheck.first(rows)
        XCTAssertEqual(first?.name, "Pi")
        XCTAssertTrue(first?.fix?.contains("hotspot") == true)
        XCTAssertEqual(rows.first { $0.name == "Sound with the phone locked" }?.level, .warn)
    }

    func testTheCarComputersOwnVerdicts() {
        XCTAssertEqual(SetupCheck.car(nil).map(\.level), [.idle])
        let report = DoctorReport(items: [["Receiver", "PROBLEM", "not on USB"], ["Clock", "note", "not synced"], ["Service", "ok", "enabled"], ["odd"]])
        XCTAssertEqual(SetupCheck.car(report).map(\.level), [.bad, .warn, .ok])
    }

    func testTheServerMessageCarriesIt() throws {
        let json = #"{"items":[["Receiver","ok","on USB"]],"age_s":12.5}"#
        let report = try JSONDecoder().decode(DoctorReport.self, from: Data(json.utf8))
        XCTAssertEqual(report.age, 12.5)
        XCTAssertEqual(report.items.first?[2], "on USB")
    }
}
