import XCTest

/// The same cases as the Android core's SupportTest, so both phones read the car computer alike.
final class SupportTests: XCTestCase {
    func testTheCarComputersDrivesAndProgressDecode() throws {
        let json = """
        {"boot":"b","seq":1,"server_mono_s":1,"state":{"snapshot_age_s":0,
         "detector":{"state":"running"},"channels":[],"matches":[],
         "drives":[{"session":"20261119-171847-301483Z_watch_382.5MHz","start":"2026-11-20T18:40:00+00:00",
         "minutes":72.4,"matches":37,"faults":2,"mb":8.1,"recordings":3}],
         "support":{"state":"uploading","sent":3100000,"total":8400000}}}
        """
        let m = try JSONDecoder().decode(ServerMessage.self, from: Data(json.utf8))
        let d = try XCTUnwrap(m.state?.drives?.first)
        XCTAssertEqual(SupportWords.drive(d, zone: TimeZone(identifier: "Europe/Amsterdam")!), "20 Nov 19:40, 72 min, 37 matches, 2 faults")
        XCTAssertEqual(SupportWords.progress(m.state?.support), "Uploading 3.1 MB of 8.4 MB…")
        XCTAssertNil(SupportWords.progress(nil))
    }

    func testTheServersListing() throws {
        let json = """
        {"packages":[{"name":"support-20261122-120000Z-aaaaaaaa.zip","size":812345,"age_s":12.5,"choice":"quick",
         "phone":["phone/app-log.txt"],"team_code":"S17-7K3Q9X"}]}
        """
        let p = try XCTUnwrap(try JSONDecoder().decode(SupportListing.self, from: Data(json.utf8)).packages.first)
        XCTAssertEqual(p.teamCode, "S17-7K3Q9X")
        XCTAssertEqual(SupportWords.size(p.size), "813 KB")
        XCTAssertEqual(SupportWords.choice(p.choice), "Quick check")
        XCTAssertFalse(p.withPlaces)
    }

    func testTheWindow() {
        XCTAssertEqual(SupportWords.window(" ", ""), .success(nil))
        XCTAssertEqual(SupportWords.window("30", "50"), .success([30, 50]))
        for (a, b) in [("50", "30"), ("x", "5"), ("0", "2000"), ("-1", "5")] {
            XCTAssertEqual(SupportWords.window(a, b), .failure(.invalid), "\(a)-\(b)")
        }
    }
}
