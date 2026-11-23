package nl.slot17.core

import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.double
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long

/** The Bluetooth link's frames, as shared/parity/ble-frames.json says the car computer makes them. */
class BluetoothTest {
    private val fixture = ProtocolJson.parseToJsonElement(
        File(System.getProperty("slot17.parity") ?: "../../shared/parity", "ble-frames.json").readText(),
    ).jsonObject
    private val key = assertNotNull(hexBytes(fixture["key"]!!.jsonPrimitive.content))

    private fun frame(name: String): Pair<BluetoothFormat.Frame, JsonObject> {
        val f = fixture["frames"]!!.jsonArray.map { it.jsonObject }.single { it["name"]!!.jsonPrimitive.content == name }
        return BluetoothFormat.decode(assertNotNull(hexBytes(f["hex"]!!.jsonPrimitive.content)), key) to
            f["decoded"]!!.jsonObject
    }

    private fun JsonObject.num(k: String): Double? = this[k]?.jsonPrimitive?.doubleOrNull

    @Test
    fun status() {
        val (got, want) = frame("status")
        val s = assertIs<BluetoothFormat.Frame.Status>(got)
        val d = want["detector"]!!.jsonObject
        assertEquals(want["boot"]!!.jsonPrimitive.long, s.boot)
        assertEquals(want["counter"]!!.jsonPrimitive.long, s.counter)
        assertEquals(d["state"]!!.jsonPrimitive.content, s.detector.state)
        assertEquals(d["windows"]!!.jsonPrimitive.int, s.detector.windows)
        assertEquals(d["hits"]!!.jsonPrimitive.int, s.detector.hits)
        assertEquals(d["quiet_alarm_windows"]!!.jsonPrimitive.int, s.detector.quietAlarmWindows)
        assertEquals(d["whole_band_windows"]!!.jsonPrimitive.int, s.detector.wholeBandWindows)
        assertEquals(d.num("heartbeat_unchanged_s")!!, s.detector.heartbeatUnchanged!!, 1e-9)
        assertEquals(d.num("floor_db")!!, s.detector.floorDB!!, 1e-9)
    }

    @Test
    fun matches() {
        val (got, want) = frame("matches")
        val rows = assertIs<BluetoothFormat.Frame.Matches>(got).matches
        val expected = want["matches"]!!.jsonArray.map { it.jsonObject }
        assertEquals(expected.size, rows.size)
        for ((row, e) in rows.zip(expected)) {
            assertEquals(e["key"]!!.jsonPrimitive.content, row.key)
            assertEquals(e["ch"]!!.jsonPrimitive.int, row.ch)
            assertEquals(e.num("strength_db"), row.strength)
            assertEquals(e["age_s"]!!.jsonPrimitive.double, row.age, 1e-9)
            assertEquals(e["rule"]?.jsonPrimitive?.contentOrNullSafe(), row.rule)
        }
    }

    @Test
    fun channels() {
        val (got, want) = frame("channels")
        val rows = assertIs<BluetoothFormat.Frame.Channels>(got).channels
        val expected = want["channels"]!!.jsonArray.map { it.jsonObject }
        assertEquals(expected.size, rows.size)
        for ((row, e) in rows.zip(expected)) {
            assertEquals(e["ch"]!!.jsonPrimitive.int, row.ch)
            assertEquals(e["freq_mhz"]!!.jsonPrimitive.double, row.freqMHz, 1e-9)
            assertEquals(e.num("strength_db"), row.strength)
            assertEquals(e["age_s"]!!.jsonPrimitive.double, row.age, 1e-9)
            assertEquals(e["hits_60s"]!!.jsonPrimitive.int, row.hits60)
            assertEquals(e["rising"]!!.jsonPrimitive.boolean, row.rising)
            assertEquals(e["call"]!!.jsonPrimitive.boolean, row.call)
            assertEquals(e["approx_time"]!!.jsonPrimitive.boolean, row.approxTime)
            assertEquals(e["activity"]?.jsonPrimitive?.contentOrNullSafe(), row.activity)
            assertEquals("bt:" + e["burst"]!!.jsonPrimitive.content, row.burstID)
        }
    }

    @Test
    fun aChangedOrForeignFrameIsRefused() {
        assertFailsWith<BluetoothFormat.FrameError> {
            BluetoothFormat.decode(assertNotNull(hexBytes(fixture["tampered"]!!.jsonPrimitive.content)), key)
        }
        val good = assertNotNull(hexBytes(fixture["frames"]!!.jsonArray[0].jsonObject["hex"]!!.jsonPrimitive.content))
        assertFailsWith<BluetoothFormat.FrameError> { BluetoothFormat.decode(good, ByteArray(32)) }
        assertFailsWith<BluetoothFormat.FrameError> { BluetoothFormat.decode(ByteArray(10), key) }
        assertEquals(fixture["key_id"]!!.jsonPrimitive.content, BluetoothFormat.keyId(key))
    }

    @Test
    fun matchKeysAreTheSameAsTheCarComputers() {
        for (m in fixture["match_keys"]!!.jsonArray.map { it.jsonObject }) {
            assertEquals(m["key"]!!.jsonPrimitive.content, MatchKey.of(m["id"]!!.jsonPrimitive.content))
        }
    }

    @Test
    fun aReplayIsRefused() {
        val acceptor = FrameAcceptor()
        for (step in fixture["sequence"]!!.jsonArray.map { it.jsonObject }) {
            assertEquals(step["accept"]!!.jsonPrimitive.boolean,
                         acceptor.accept(step["boot"]!!.jsonPrimitive.long, step["counter"]!!.jsonPrimitive.long), "$step")
        }
    }

    @Test
    fun aMatchThatComesBothWaysIsDecidedOnce() {
        val serverId = fixture["match_keys"]!!.jsonArray[0].jsonObject["id"]!!.jsonPrimitive.content
        val bluetooth = assertIs<BluetoothFormat.Frame.Matches>(frame("matches").first).matches.first()
        val engine = AlertEngine()
        engine.ingest(listOf(bluetooth), null, emptyList(), "pi/s1", receivedAt = 10.0)
        engine.ingest(listOf(MatchRow(id = serverId, ch = bluetooth.ch, strength = bluetooth.strength, age = 0.3)),
                      null, emptyList(), "pi/s1")
        assertEquals(1, engine.waitingCount)
    }

    @Test
    fun routeStats() {
        val stats = RouteStats()
        stats.arrived("a", RouteStats.Route.BLUETOOTH, 10.0)
        stats.arrived("a", RouteStats.Route.SERVER, 10.4)
        stats.arrived("b", RouteStats.Route.SERVER, 20.0)
        stats.arrived("b", RouteStats.Route.BLUETOOTH, 20.1)
        stats.arrived("c", RouteStats.Route.BLUETOOTH, 30.0)
        stats.arrived("c", RouteStats.Route.BLUETOOTH, 30.0)
        assertEquals(1, stats.bluetoothFirst)
        assertEquals(1, stats.serverFirst)
        assertEquals(0.4, assertNotNull(stats.medianLead), 1e-9)
        stats.settle(now = 100.0)
        assertEquals(1, stats.bluetoothOnly)
        assertNull(RouteStats().medianLead)
        assertTrue(hexBytes("zz") == null && hexBytes("abc") == null)
    }

    @Test
    fun theFeedTakesCheckedFramesOnceAndSaysWhenItIsLive() {
        val feed = BluetoothFeed()
        val status = assertNotNull(hexBytes(fixture["frames"]!!.jsonArray[0].jsonObject["hex"]!!.jsonPrimitive.content))
        val matches = assertNotNull(hexBytes(fixture["frames"]!!.jsonArray[1].jsonObject["hex"]!!.jsonPrimitive.content))
        assertIs<BluetoothFeed.Received.Refused>(feed.receive(status, 1.0))      // no key yet
        feed.key = key
        assertEquals(BluetoothFeed.Received.State, feed.receive(status, 2.0))
        assertTrue(feed.live(2.0))
        assertTrue(!feed.live(2.0 + LinkTracker.CAR_STALE + 0.1))
        assertIs<BluetoothFeed.Received.Refused>(feed.receive(status, 3.0))      // the same frame again
        val got = assertIs<BluetoothFeed.Received.Matches>(feed.receive(matches, 4.0))
        assertEquals(2, got.matches.size)
        assertEquals("running", feed.piState(null, 4.0)?.detector?.state)
        assertEquals(2, feed.refused)
    }

    @Test
    fun controlFramesAreTheSameAsTheCarComputersAndTheReplyReads() {
        for (c in fixture["controls"]!!.jsonArray.map { it.jsonObject }) {
            val action = if (c["action"]!!.jsonPrimitive.content == "rename") BluetoothFormat.Action.RENAME else BluetoothFormat.Action.OFF
            val made = BluetoothFormat.control(key, c["boot"]!!.jsonPrimitive.long, c["counter"]!!.jsonPrimitive.long,
                                               action, c["name"]!!.jsonPrimitive.content)
            assertEquals(c["hex"]!!.jsonPrimitive.content, made.hex())
        }
        val (got, want) = frame("reply")
        val reply = assertIs<BluetoothFormat.Frame.Reply>(got)
        assertEquals(BluetoothFormat.Action.RENAME, reply.action)
        assertEquals(want["ok"]!!.jsonPrimitive.boolean, reply.ok)
        assertEquals(want["text"]!!.jsonPrimitive.content, reply.text)
        val feed = BluetoothFeed(key)
        assertNull(feed.control(BluetoothFormat.Action.OFF))                 // nothing heard yet
        val status = assertNotNull(hexBytes(fixture["frames"]!!.jsonArray[0].jsonObject["hex"]!!.jsonPrimitive.content))
        feed.receive(status, 1.0)
        assertNotNull(feed.control(BluetoothFormat.Action.RENAME, "Car 2"))
    }

    @Test
    fun namesAndWords() {
        for (good in listOf("Slot17", "Car 2", "VW.Golf_7-GTI", "x".repeat(20))) assertTrue(BluetoothName.valid(good), good)
        for (bad in listOf("", " a", "a ", "x".repeat(21), "a:b", "a;b", "-x", "Éva")) assertTrue(!BluetoothName.valid(bad), bad)
        assertEquals("Off", BluetoothWords.car(BluetoothState(on = false)))
        assertEquals("A phone is connected", BluetoothWords.car(BluetoothState(on = true, state = "connected")))
        val decoded = ServerMessage.decode("""{"boot":"b","seq":1,"server_mono_s":1,"state":{"snapshot_age_s":0,
            "detector":{"state":"running"},"channels":[],"matches":[],"bluetooth":{"on":true,"state":"advertising",
            "name":"Car 2","key_id":"630dcd29","subscribed":false,"frames":3,"mtu":185,"phones_pair":true}}}""")
        assertEquals("Car 2", decoded?.state?.bluetooth?.name)
        assertEquals(185, decoded?.state?.bluetooth?.mtu)
        assertEquals(true, decoded?.state?.bluetooth?.phonesPair)
        assertEquals(null, BluetoothState(on = true).phonesPair)        // an older car computer says nothing
    }

    private fun kotlinx.serialization.json.JsonPrimitive.contentOrNullSafe(): String? = if (isString) content else null
}
