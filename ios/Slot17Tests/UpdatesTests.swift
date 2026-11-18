import XCTest

final class UpdatesTests: XCTestCase {
    func testVersionsCompareByNumber() {
        XCTAssertTrue(Updates.isNewer("1.10.0", than: "1.9.9"))
        XCTAssertTrue(Updates.isNewer("1.6.1", than: "1.6"))
        XCTAssertFalse(Updates.isNewer("1.6.1", than: "1.6.1"))
        XCTAssertFalse(Updates.isNewer("1.5.9", than: "1.6.0"))
    }

    func testOnlyANewerKnownVersionIsMentioned() {
        XCTAssertEqual(Updates.newer("1.7.0", than: "1.6.1"), "1.7.0")
        XCTAssertNil(Updates.newer("1.6.1", than: "1.6.1"))
        XCTAssertNil(Updates.newer(nil, than: "1.6.1"), "an older release without versions.json")
        XCTAssertNil(Updates.newer("1.3.0", than: nil), "a car computer that does not say its version")
    }

    func testTheReleaseDecodesAndIsOptional() throws {
        let with = #"{"boot":"b","seq":1,"server_mono_s":5,"state":null,"release":{"software":"1.3.0","app":"1.7.0","tag":"v1.3.0","url":"https://github.com/efeorman/Slot17/releases/tag/v1.3.0"}}"#
        let m = try JSONDecoder().decode(ServerMessage.self, from: Data(with.utf8))
        XCTAssertEqual(m.release?.app, "1.7.0")
        let without = #"{"boot":"b","seq":1,"server_mono_s":5,"state":null}"#
        XCTAssertNil(try JSONDecoder().decode(ServerMessage.self, from: Data(without.utf8)).release)
    }

    func testTheAnnouncementNamesWhatIsNew() {
        XCTAssertEqual(Updates.announcement(app: "1.7.0", software: nil), "app 1.7.0")
        XCTAssertEqual(Updates.announcement(app: "1.7.0", software: "1.3.0"), "app 1.7.0 and car computer software 1.3.0")
        XCTAssertNil(Updates.announcement(app: nil, software: nil))
    }
}
