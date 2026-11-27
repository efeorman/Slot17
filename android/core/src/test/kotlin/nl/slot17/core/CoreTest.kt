package nl.slot17.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

private fun message(boot: String = "b1", seq: Int, mono: Double, state: String = "running", age: Double? = 0.1) =
    ServerMessage(boot, seq, mono, age,
        PiState(source = "pi", session = "s1", snapshotAge = 0.2, detector = DetectorState(state, windows = 10),
            channels = emptyList(), matches = emptyList()))

class FmtTest {
    @Test fun roundsLikeSwift() {
        assertEquals("0.2", Fmt.fixed(0.25, 1))   // exact tie: to even
        assertEquals("0.3", Fmt.fixed(0.35, 1))   // stored as 0.34999…; Java's String.format would say 0.4
        assertEquals("15.2", Fmt.fixed(15.25, 1)) // exact tie in binary: to even, as printf does
        assertEquals("2", Fmt.fixed(2.5, 0))
        assertEquals("4", Fmt.fixed(3.5, 0))
        assertEquals("-0.0", Fmt.fixed(-0.04, 1))
        assertEquals("22", Fmt.fixed(22.0, 0))
    }

    @Test fun roundAwayFromZero() {
        assertEquals(3.0, Fmt.roundAway(2.5))
        assertEquals(-3.0, Fmt.roundAway(-2.5))
        assertEquals(2.0, Fmt.roundAway(2.4))
    }
}

class SseTest {
    @Test fun splitsAcrossChunks() {
        val p = SseParser()
        assertEquals(emptyList(), p.feed("data: {\"a\":".toByteArray()))
        assertEquals(listOf("{\"a\":1}"), p.feed("1}\n\n: keepalive\n\ndata:x\ndata: y\n\n".toByteArray()).take(1))
        val q = SseParser()
        assertEquals(listOf("x\ny"), q.feed("event: m\ndata:x\ndata: y\n\n".toByteArray()))
    }

    @Test fun skipsInvalidUtf8() {
        val p = SseParser()
        assertEquals(listOf("ok"), p.feed(byteArrayOf(100, 97, 116, 97, 58, -1, 10, 10) + "data: ok\n\n".toByteArray()))
    }
}

class LinkTrackerTest {
    @Test fun ordersAndRefuses() {
        val t = LinkTracker()
        assertEquals(LinkTracker.Acceptance.FRESH_AND_NEW_SCOPE, t.accept(message(seq = 1, mono = 10.0), 100.0))
        assertEquals(LinkTracker.Acceptance.REFUSED, t.accept(message(seq = 0, mono = 11.0), 100.1), "older seq")
        assertEquals(LinkTracker.Acceptance.REPEATED, t.accept(message(seq = 1, mono = 12.0), 100.2), "repeat of the same push")
        assertEquals(LinkTracker.Acceptance.FRESH, t.accept(message(seq = 2, mono = 13.0), 100.3))
        assertEquals(LinkTracker.Acceptance.REFUSED, t.accept(message(boot = "b2", seq = 1, mono = 1.0), 100.4),
            "a new server boot needs a clock sample first")
    }

    @Test fun liveOnlyWithAClockSample() {
        val t = LinkTracker()
        t.accept(message(seq = 1, mono = 10.0), 100.0)
        assertFalse(t.live(100.0), "no timing sample yet")
        assertTrue(t.mayHoldMatches(100.0))
        t.applyProbe(message(seq = 2, mono = 10.5), begin = 100.4, end = 100.5)
        assertTrue(t.live(100.6))
        assertTrue(t.carAge(100.6) < 1.0)
        assertFalse(t.live(100.6 + LinkTracker.LINK_LOST + 1), "nothing received for too long")
    }

    /** 1.8.0: with both routes fresh, Bluetooth's data comes straight from the car computer and goes first. */
    @Test fun bluetoothGoesFirstWhileItsDataIsFresh() {
        assertTrue(LinkTracker.bluetoothFirst(true, 1.2, serverFresh = true), "both fresh")
        assertTrue(LinkTracker.bluetoothFirst(true, LinkTracker.BLUETOOTH_FIRST_WITHIN, serverFresh = true))
        assertFalse(LinkTracker.bluetoothFirst(true, 6.0, serverFresh = true), "a stalled radio link")
        assertTrue(LinkTracker.bluetoothFirst(true, 6.0, serverFresh = false), "nothing fresher")
        assertFalse(LinkTracker.bluetoothFirst(false, 0.5, serverFresh = true), "no running detector over it")
        assertFalse(LinkTracker.bluetoothFirst(false, 0.5, serverFresh = false))
    }
}

class StatusTest {
    private val s = AlertSettings()

    @Test fun aircraftChannels() {
        // ECC/DEC/(06)05: 384.750-385.000 MHz is channels 190-199 on the grid (1.8.0).
        val air = (0 until 200).filter { ch -> (380.0125 + ch * 0.025).let { it >= 384.75 - 1e-9 && it < 385.0 } }
        assertEquals(air, (0 until 200).filter { Aircraft.isAir(it) })
        assertEquals(190..199, Aircraft.channels)
        assertTrue(MeterRow(190, 384.7625, 30.0, 1.0, 1, false).air)
        assertFalse(MeterRow(189, 384.7375, 30.0, 1.0, 1, false).air)
        assertFalse(Aircraft.isAir(null))
    }

    @Test fun meters() {
        assertEquals(0, Meters.level(null))
        assertEquals(1, Meters.level(6.5))
        assertEquals(9, Meters.level(49.9))
        assertEquals(10, Meters.level(50.0))
        assertEquals(MeterColour.RED, Meters.colour(35.0, s))
        assertEquals(MeterColour.YELLOW, Meters.colour(34.9, s), "the chime level is the server's 35 dB by default")
        assertEquals(MeterColour.YELLOW, Meters.colour(12.0, s))
        assertEquals(MeterColour.WEAK, Meters.colour(11.9, s))
        assertEquals(MeterColour.YELLOW, Meters.colour(25.0, s.copy(quiet = true)))
        val rows = listOf(MeterRow(1, 380.0, 40.0, 20.0, 1, false), MeterRow(2, 380.0, 10.0, 5.0, 1, false),
            MeterRow(3, 380.0, 30.0, 3.0, 1, false), MeterRow(4, 380.0, 60.0, 61.0, 1, false))
        assertEquals(listOf(3, 2, 1), Meters.pick(rows).map { it.ch })
    }

    @Test fun words() {
        assertEquals("never", Format.ago(Double.POSITIVE_INFINITY))
        assertEquals("now", Format.ago(0.5))
        assertEquals("3 s ago", Format.ago(2.5))
        assertEquals("2 min ago", Format.ago(90.0))
        assertEquals("1 h 5 min", Format.duration(3900.0))
        assertEquals("895fda1; an update installs at the next start", Format.software(UpdateState("staged", "895fda1c0ffee")))
    }

    @Test fun headline() {
        val t = LinkTracker()
        assertEquals("Not set up", StatusBuilder.headline(t, 0.0, null, hasCredentials = false).title)
        assertEquals("Waiting for the car computer", StatusBuilder.headline(t, 0.0, null, hasCredentials = false, bluetoothSetUp = true).title)
        assertEquals("Connecting…", StatusBuilder.headline(t, 0.0, null, true).title)
        t.accept(message(seq = 1, mono = 10.0), 100.0)
        t.applyProbe(message(seq = 2, mono = 10.5), begin = 100.4, end = 100.5)
        val h = StatusBuilder.headline(t, 100.6, 1.23, true)
        assertEquals(Level.OK, h.level)
        assertEquals("Delay up to 1.3 s after the burst", h.detail)
        assertEquals(listOf("Connected", "Connected", "Running", "No fault"), StatusBuilder.chain(t, 100.6, true).map { it.detail })
    }
}

class RemoteConfigTest {
    @Test fun builtInSoundsAreValid() {
        for (kind in SoundKind.entries) assertNull(kind.builtIn.problem, kind.name)
        val samples = SoundSynth.render(SoundKind.CHIME.builtIn)
        assertEquals(0.65f, samples.maxOf { kotlin.math.abs(it) }, 1e-4f)
    }

    @Test fun problemsAndApply() {
        val (version, config) = assertNotNull(RemoteConfig.decode(
            """{"version":"v7","config":{"rules":{"red_db":25,"yellow_db":30},"sounds":{"beep":{"wav":"x.wav"}},
               "display":{"chime_colour":"#22CC44"}}}"""))
        assertEquals("v7", version)
        assertTrue("yellow_db above red_db" in config.problems)
        assertTrue("unknown sound beep" in config.problems)
        assertTrue("chime_colour is green, which reads as safe" in config.problems)
        val applied = AlertSettings().apply(config)
        assertEquals(25.0, applied.red)
        assertEquals(25.0, applied.yellow, "yellow never above red")
        val mine = AlertSettings(red = 20.0, overrides = setOf("red")).apply(config)
        assertEquals(20.0, mine.red, "the operator's own value stays")
    }
}

class LinksTest {
    @Test fun parse() {
        assertEquals(ServerLink("http://1.2.3.4:5732", "abcdefgh_-12"), ServerLink.parse("  http://1.2.3.4:5732/?key=abcdefgh_-12 \n"))
        assertEquals(ServerLink("https://k7f2q.slot17.cloud", "abcdefgh"), ServerLink.parse("HTTPS://k7f2q.slot17.cloud?key=abcdefgh"))
        assertNull(ServerLink.parse("http://host/?key=short"))
        assertNull(ServerLink.parse("ftp://host/?key=abcdefghij"))
        assertNull(ServerLink.parse("http://host/?key=abc%20defghij"))
        assertNull(ServerLink.parse("not a link"))
    }

    @Test fun marks() {
        val alerts = listOf(AlertRecord("a", 1_000, 5, 55.0, 1.0, SoundKind.URGENT, ""), AlertRecord("b", 50_000, 6, 30.0, 1.0, SoundKind.CHIME, ""))
        val sightings = listOf(Sighting("s1", Sighting.Label.POLICE, "", 20_000, Sighting.Phase.SAVED, "", false),
            Sighting("s2", Sighting.Label.FIRE, "", 400_000, Sighting.Phase.SAVED, "", false))
        val m = MarkSummary.make(alerts, sightings, 120.0)
        assertEquals(MarkSummary.Row(1, 1, 0), m.urgent)
        assertEquals(MarkSummary.Row(1, 0, 0), m.chime)
        assertEquals(2, m.vehiclesMarked)
        assertEquals(1, m.vehiclesWithoutSound)
        assertTrue(Sighting.isRepeat(Sighting.Label.POLICE, 22_000, sightings))
        assertFalse(Sighting.isRepeat(Sighting.Label.POLICE, 24_000, sightings))
    }
}

class NoticesTest {
    @Test fun versions() {
        assertTrue(Updates.isNewer("1.10.0", "1.9.0"))
        assertFalse(Updates.isNewer("1.2.9", "1.2.9"))
        assertEquals("1.6.2", Updates.newer("1.6.2", "1.6.1"))
        assertNull(Updates.newer("1.6.2", ""))
        assertEquals("app 1.6.2 and car computer software 1.3.0", Updates.announcement("1.6.2", "1.3.0"))
    }

    @Test fun faultEpisode() {
        val f = FaultNotices()
        val degraded = FaultNotices.Input("degraded", listOf("sample loss"))
        assertEquals("Slot17: sample loss", f.step(degraded, 0.0)?.title)
        assertNull(f.step(degraded, 70.0), "same fault: no reminder")
        assertNull(f.step(FaultNotices.Input("starting"), 140.0), "starting neither clears nor reports")
        assertEquals("Slot17: running normally again", f.step(FaultNotices.Input("running"), 200.0)?.title)
        assertNull(f.step(FaultNotices.Input("stopped", expectingSilence = true), 300.0))
        assertNull(f.step(FaultNotices.Input(null, receiverQuiet = true), 400.0), "no fresh data: no notice")
    }
}
