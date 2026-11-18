import XCTest

final class SharedMarksTests: XCTestCase {
    private let tap = Date(timeIntervalSince1970: 1_800_000_000)
    private var last: SharedMarks.Last { .init(kind: "police", title: "Police", at: tap) }

    func testTheTappedButtonIsTickedForAMinuteAndTheLineStaysHalfAnHour() {
        XCTAssertEqual(MarkWidgetModel.look(last, at: tap.addingTimeInterval(5)), .init(tickedKind: "police", line: last))
        XCTAssertEqual(MarkWidgetModel.look(last, at: tap.addingTimeInterval(61)), .init(tickedKind: nil, line: last))
        XCTAssertEqual(MarkWidgetModel.look(last, at: tap.addingTimeInterval(31 * 60)), .init(tickedKind: nil, line: nil))
        XCTAssertEqual(MarkWidgetModel.look(nil, at: tap), .init(tickedKind: nil, line: nil))
    }

    func testTheWidgetRedrawsExactlyWhenTheTickAndTheLineGo() {
        let now = tap.addingTimeInterval(10)
        XCTAssertEqual(MarkWidgetModel.changes(last, from: now), [now, tap.addingTimeInterval(60), tap.addingTimeInterval(1800)])
        let later = tap.addingTimeInterval(120)
        XCTAssertEqual(MarkWidgetModel.changes(last, from: later), [later, tap.addingTimeInterval(1800)])
        XCTAssertEqual(MarkWidgetModel.changes(nil, from: now), [now])
    }

    func testTheGrantedGroupIsReadFromAProvisioningProfile() {
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Entitlements</key><dict>\
        <key>com.apple.security.application-groups</key><array><string>group.other</string>\
        <string>group.nl.slot17.app.ABCDE12345</string></array></dict></dict></plist>
        """
        var data = Data([0x30, 0x80, 0x06, 0x09])            // the signed container's first bytes
        data.append(plist.data(using: .utf8)!)
        data.append(Data([0x00, 0x00, 0xA0, 0x82]))
        XCTAssertEqual(SharedMarks.groups(inProfile: data).first, "group.nl.slot17.app.ABCDE12345")
        XCTAssertEqual(SharedMarks.groups(inProfile: Data("no plist here".utf8)), [])
    }
}
