import XCTest

/// The Bluetooth link's frames, as shared/parity/ble-frames.json says the car computer makes them (1.4.0).
final class BluetoothTests: XCTestCase {
    private var fixture: [String: Any] = [:]
    private var key = Data()

    override func setUpWithError() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try XCTUnwrap(try? Data(contentsOf: root.appendingPathComponent("shared/parity/ble-frames.json")),
                                 "shared/parity/ble-frames.json is missing")
        fixture = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        key = try XCTUnwrap(hexData(try XCTUnwrap(fixture["key"] as? String)))
    }

    private func frame(_ name: String) throws -> (BluetoothFormat.Frame, [String: Any]) {
        let frames = try XCTUnwrap(fixture["frames"] as? [[String: Any]])
        let f = try XCTUnwrap(frames.first { $0["name"] as? String == name })
        let bytes = try XCTUnwrap(hexData(try XCTUnwrap(f["hex"] as? String)))
        return (try BluetoothFormat.decode(bytes, key: key), try XCTUnwrap(f["decoded"] as? [String: Any]))
    }

    func testStatus() throws {
        let (got, want) = try frame("status")
        guard case .status(let boot, let counter, let d) = got else { return XCTFail("not a status frame") }
        let w = try XCTUnwrap(want["detector"] as? [String: Any])
        XCTAssertEqual(Int(boot), want["boot"] as? Int)
        XCTAssertEqual(Int(counter), want["counter"] as? Int)
        XCTAssertEqual(d.state, w["state"] as? String)
        XCTAssertEqual(d.windows, w["windows"] as? Int)
        XCTAssertEqual(d.hits, w["hits"] as? Int)
        XCTAssertEqual(d.quietAlarmWindows, w["quiet_alarm_windows"] as? Int)
        XCTAssertEqual(d.wholeBandWindows, w["whole_band_windows"] as? Int)
        XCTAssertEqual(d.heartbeatUnchanged ?? -1, try XCTUnwrap(w["heartbeat_unchanged_s"] as? Double), accuracy: 1e-9)
        XCTAssertEqual(d.floorDB ?? 0, try XCTUnwrap(w["floor_db"] as? Double), accuracy: 1e-9)
    }

    func testMatches() throws {
        let (got, want) = try frame("matches")
        guard case .matches(_, _, let rows) = got else { return XCTFail("not a matches frame") }
        let expected = try XCTUnwrap(want["matches"] as? [[String: Any]])
        XCTAssertEqual(rows.count, expected.count)
        for (row, e) in zip(rows, expected) {
            XCTAssertEqual(row.key, e["key"] as? String)
            XCTAssertEqual(row.ch, e["ch"] as? Int)
            XCTAssertEqual(row.strength, e["strength_db"] as? Double)
            XCTAssertEqual(row.age, try XCTUnwrap(e["age_s"] as? Double), accuracy: 1e-9)
            XCTAssertEqual(row.rule, e["rule"] as? String)
        }
    }

    func testChannels() throws {
        let (got, want) = try frame("channels")
        guard case .channels(_, _, let rows) = got else { return XCTFail("not a channels frame") }
        let expected = try XCTUnwrap(want["channels"] as? [[String: Any]])
        XCTAssertEqual(rows.count, expected.count)
        for (row, e) in zip(rows, expected) {
            XCTAssertEqual(row.ch, e["ch"] as? Int)
            XCTAssertEqual(row.freqMHz, try XCTUnwrap(e["freq_mhz"] as? Double), accuracy: 1e-9)
            XCTAssertEqual(row.strength, e["strength_db"] as? Double)
            XCTAssertEqual(row.age, try XCTUnwrap(e["age_s"] as? Double), accuracy: 1e-9)
            XCTAssertEqual(row.hits60, e["hits_60s"] as? Int)
            XCTAssertEqual(row.rising, e["rising"] as? Bool)
            XCTAssertEqual(row.call, e["call"] as? Bool)
            XCTAssertEqual(row.approxTime, e["approx_time"] as? Bool)
            XCTAssertEqual(row.activity, e["activity"] as? String)
            XCTAssertEqual(row.burstID, "bt:" + (e["burst"] as? String ?? ""))
        }
    }

    func testAChangedOrForeignFrameIsRefused() throws {
        let tampered = try XCTUnwrap(hexData(try XCTUnwrap(fixture["tampered"] as? String)))
        XCTAssertThrowsError(try BluetoothFormat.decode(tampered, key: key))
        let frames = try XCTUnwrap(fixture["frames"] as? [[String: Any]])
        let good = try XCTUnwrap(hexData(try XCTUnwrap(frames[0]["hex"] as? String)))
        XCTAssertThrowsError(try BluetoothFormat.decode(good, key: Data(count: 32)))
        XCTAssertThrowsError(try BluetoothFormat.decode(Data(count: 10), key: key))
        XCTAssertEqual(BluetoothFormat.keyID(key), fixture["key_id"] as? String)
    }

    func testMatchKeysAreTheSameAsTheCarComputers() throws {
        for m in try XCTUnwrap(fixture["match_keys"] as? [[String: Any]]) {
            XCTAssertEqual(MatchKey.of(try XCTUnwrap(m["id"] as? String)), m["key"] as? String)
        }
    }

    func testAReplayIsRefused() throws {
        var acceptor = FrameAcceptor()
        for step in try XCTUnwrap(fixture["sequence"] as? [[String: Any]]) {
            let boot = UInt32(try XCTUnwrap(step["boot"] as? Int))
            let counter = UInt32(try XCTUnwrap(step["counter"] as? Int))
            XCTAssertEqual(acceptor.accept(boot: boot, counter: counter), step["accept"] as? Bool, "\(step)")
        }
    }

    func testAMatchThatComesBothWaysIsDecidedOnce() throws {
        guard case .matches(_, _, let rows) = try frame("matches").0, let bluetooth = rows.first else { return XCTFail() }
        let ids = try XCTUnwrap(fixture["match_keys"] as? [[String: Any]])
        let serverID = try XCTUnwrap(ids.first?["id"] as? String)
        let engine = AlertEngine()
        engine.ingest([bluetooth], frame: nil, channels: [], scope: "pi/s1", receivedAt: 10)
        engine.ingest([MatchRow(id: serverID, ch: bluetooth.ch, strength: bluetooth.strength, age: 0.3)],
                      frame: nil, channels: [], scope: "pi/s1")
        XCTAssertEqual(engine.waitingCount, 1)
    }

    func testTheFeedTakesCheckedFramesOnceAndSaysWhenItIsLive() throws {
        let frames = try XCTUnwrap(fixture["frames"] as? [[String: Any]])
        let status = try XCTUnwrap(hexData(try XCTUnwrap(frames[0]["hex"] as? String)))
        let matches = try XCTUnwrap(hexData(try XCTUnwrap(frames[1]["hex"] as? String)))
        var feed = BluetoothFeed()
        XCTAssertEqual(feed.receive(status, now: 1), .refused("no key for this car computer yet"))
        feed.key = key
        XCTAssertEqual(feed.receive(status, now: 2), .state)
        XCTAssertTrue(feed.live(2))
        XCTAssertFalse(feed.live(2 + LinkTracker.carStale + 0.1))
        XCTAssertEqual(feed.receive(status, now: 3), .refused("a frame it had already sent"))
        guard case .matches(let keys) = feed.receive(matches, now: 4) else { return XCTFail("no matches") }
        XCTAssertEqual(keys.count, 2)
        XCTAssertEqual(feed.matches.count, 2)
        XCTAssertEqual(feed.piState(base: nil, now: 4)?.detector.state, "running")
    }

    func testControlFramesAreTheSameAsTheCarComputersAndTheReplyReads() throws {
        for c in try XCTUnwrap(fixture["controls"] as? [[String: Any]]) {
            let action: BluetoothFormat.Action = c["action"] as? String == "rename" ? .rename : .off
            let made = BluetoothFormat.control(key: key, boot: UInt32(try XCTUnwrap(c["boot"] as? Int)),
                                               counter: UInt32(try XCTUnwrap(c["counter"] as? Int)),
                                               action: action, name: c["name"] as? String ?? "")
            XCTAssertEqual(BluetoothFormat.hex(made), c["hex"] as? String)
        }
        let (got, want) = try frame("reply")
        guard case .reply(_, _, let action, let ok, let text) = got else { return XCTFail("not a reply frame") }
        XCTAssertEqual(action, .rename)
        XCTAssertEqual(ok, want["ok"] as? Bool)
        XCTAssertEqual(text, want["text"] as? String)
        var feed = BluetoothFeed(key: key)
        XCTAssertNil(feed.control(.off))
        let frames = try XCTUnwrap(fixture["frames"] as? [[String: Any]])
        _ = feed.receive(try XCTUnwrap(hexData(try XCTUnwrap(frames[0]["hex"] as? String))), now: 1)
        XCTAssertNotNil(feed.control(.rename, name: "Car 2"))
    }

    func testRouteStats() {
        var stats = RouteStats()
        stats.arrived("a", .bluetooth, at: 10)
        stats.arrived("a", .server, at: 10.4)
        stats.arrived("b", .server, at: 20)
        stats.arrived("b", .bluetooth, at: 20.1)
        stats.arrived("c", .bluetooth, at: 30)
        XCTAssertEqual(stats.bluetoothFirst, 1)
        XCTAssertEqual(stats.serverFirst, 1)
        XCTAssertEqual(stats.medianLead ?? 0, 0.4, accuracy: 1e-9)
        stats.settle(now: 100)
        XCTAssertEqual(stats.bluetoothOnly, 1)
        XCTAssertNil(RouteStats().medianLead)
    }

    func testNamesAndTheCarComputersReport() throws {
        for good in ["Slot17", "Car 2", "VW.Golf_7-GTI", String(repeating: "x", count: 20)] { XCTAssertTrue(BluetoothName.valid(good), good) }
        for bad in ["", " a", "a ", String(repeating: "x", count: 21), "a:b", "a;b", "-x", "Éva"] { XCTAssertFalse(BluetoothName.valid(bad), bad) }
        let json = """
        {"boot":"b","seq":1,"server_mono_s":1,"state":{"snapshot_age_s":0,"detector":{"state":"running"},"channels":[],
        "matches":[],"bluetooth":{"on":true,"state":"advertising","name":"Car 2","key_id":"630dcd29","subscribed":false,
        "frames":3,"mtu":185}}}
        """
        let message = try JSONDecoder().decode(ServerMessage.self, from: Data(json.utf8))
        XCTAssertEqual(message.state?.bluetooth?.name, "Car 2")
        XCTAssertEqual(message.state?.bluetooth?.mtu, 185)
        XCTAssertEqual(BluetoothWords.car(message.state?.bluetooth), "On, waiting for a phone")
    }
}
