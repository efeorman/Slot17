package nl.slot17.core

import java.time.ZoneId
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class SupportTest {
    @Test
    fun theCarComputersDrivesAndProgressDecode() {
        val m = ServerMessage.decode("""{"boot":"b","seq":1,"server_mono_s":1,"state":{"snapshot_age_s":0,
            "detector":{"state":"running"},"channels":[],"matches":[],
            "drives":[{"session":"20261119-171847-301483Z_watch_382.5MHz","start":"2026-11-20T18:40:00+00:00",
            "minutes":72.4,"matches":37,"faults":2,"mb":8.1,"recordings":3}],
            "support":{"state":"uploading","sent":3100000,"total":8400000}}}""")
        val d = m?.state?.drives?.single()
        assertEquals("20 Nov 19:40, 72 min, 37 matches, 2 faults", SupportWords.drive(d!!, ZoneId.of("Europe/Amsterdam")))
        assertEquals("Uploading 3.1 MB of 8.4 MB…", SupportWords.progress(m.state?.support))
        assertNull(SupportWords.progress(null))
    }

    @Test
    fun theServersListing() {
        val l = ProtocolJson.decodeFromString(SupportListing.serializer(), """{"packages":[{"name":"support-20261122-120000Z-aaaaaaaa.zip",
            "size":812345,"age_s":12.5,"choice":"quick","phone":["phone/app-log.txt"],"team_code":"S17-7K3Q9X"}]}""")
        val p = l.packages.single()
        assertEquals(("S17-7K3Q9X" to "813 KB"), (p.teamCode to SupportWords.size(p.size)))
        assertEquals("Quick check", SupportWords.choice(p.choice))
    }

    @Test
    fun theWindow() {
        assertNull(SupportWords.window(" ", "").getOrThrow())
        assertEquals(listOf(30, 50), SupportWords.window("30", "50").getOrThrow())
        for ((a, b) in listOf("50" to "30", "x" to "5", "0" to "2000", "-1" to "5")) assertTrue(SupportWords.window(a, b).isFailure, "$a-$b")
    }
}
