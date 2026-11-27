package nl.slot17.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class SetupCheckTest {
    private val good = listOf(
        ChainItem("server", "Server", Level.OK, "Connected"),
        ChainItem("pi", "Pi", Level.OK, "Connected"),
        ChainItem("detector", "Detector", Level.OK, "Running"),
        ChainItem("receiver", "Receiver", Level.OK, "No fault"),
    )

    @Test fun noLinkIsTheOnlyThingSaid() {
        val rows = SetupCheck.phone(false, good, true, true, true)
        assertEquals(1, rows.size)
        assertEquals(Level.BAD, rows[0].level)
        assertTrue(rows[0].fix!!.contains("/server create"))
    }

    @Test fun allGood() {
        val rows = SetupCheck.phone(true, good, true, true, true)
        val bluetoothOnly = SetupCheck.phone(false, good, true, true, true, bluetooth = true)
        assertEquals(Level.IDLE, bluetoothOnly.first().level, "without a server, Bluetooth is enough (27 November 2026)")
        assertEquals(rows.size, bluetoothOnly.size)
        assertTrue(rows.all { it.level == Level.OK })
        assertNull(SetupCheck.first(rows))
    }

    @Test fun aBrokenLinkGetsItsFixAndComesFirst() {
        val chain = good.map { if (it.id == "pi") it.copy(level = Level.BAD, detail = "Not heard") else it }
        val rows = SetupCheck.phone(true, chain, false, true, true)
        val first = assertNotNull(SetupCheck.first(rows))
        assertEquals("Pi", first.name)
        assertTrue(first.fix!!.contains("hotspot"))
        assertEquals(Level.WARN, rows.first { it.name == "Notifications" }.level)
    }

    @Test fun theCarComputersOwnVerdicts() {
        assertEquals(Level.IDLE, SetupCheck.car(null).single().level)
        val rows = SetupCheck.car(DoctorReport(listOf(listOf("Receiver", "PROBLEM", "not on USB"), listOf("Clock", "note", "not synced"),
            listOf("Service", "ok", "enabled"), listOf("odd"))))
        assertEquals(listOf(Level.BAD, Level.WARN, Level.OK), rows.map { it.level })
    }

    @Test fun theServerMessageCarriesIt() {
        val json = """{"boot":"b","seq":1,"server_mono_s":1.0,"server_age_s":0.5,"state":{"snapshot_age_s":0.1,
            "detector":{"state":"running"},"channels":[],"matches":[],"doctor":{"items":[["Receiver","ok","on USB"]],"age_s":12.5}}}"""
        val m = ProtocolJson.decodeFromString(ServerMessage.serializer(), json)
        assertEquals(12.5, m.state?.doctor?.age)
        assertEquals("on USB", m.state?.doctor?.items?.single()?.get(2))
    }
}
