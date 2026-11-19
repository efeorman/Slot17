package nl.slot17.core

import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertTrue
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** Runs shared/parity: the decisions the iPhone app makes too. */
class ParityTest {
    private val dir = File(System.getProperty("slot17.parity") ?: "../../shared/parity")

    private fun expectText(e: JsonObject): String =
        e["played"]?.let { "${e["id"]!!.jsonPrimitive.content} played ${it.jsonPrimitive.content}" }
            ?: "${e["id"]!!.jsonPrimitive.content} silent: ${e["silent"]!!.jsonPrimitive.content}"

    private fun actualText(d: AlertDecision): String = when (val o = d.outcome) {
        is AlertDecision.Outcome.Played -> "${d.id} played ${o.kind.name.lowercase()}"
        is AlertDecision.Outcome.Silent -> "${d.id} silent: ${o.reason}"
    }

    @Test
    fun alertScenarios() {
        val root = ProtocolJson.parseToJsonElement(File(dir, "alert-scenarios.json").readText()).jsonObject
        val scenarios = root["scenarios"]!!.jsonArray
        assertTrue(scenarios.size >= 20, "all scenarios are read")
        for (sc in scenarios.map { it.jsonObject }) {
            val name = sc["name"]!!.jsonPrimitive.content
            val settings = ProtocolJson.decodeFromJsonElement(AlertSettings.serializer(), sc["settings"] ?: JsonObject(emptyMap()))
            val engine = AlertEngine()
            for ((i, step) in sc["steps"]!!.jsonArray.map { it.jsonObject }.withIndex()) {
                fun str(key: String) = step[key]?.jsonPrimitive?.contentOrNull
                fun bool(key: String, default: Boolean) = step[key]?.jsonPrimitive?.boolean ?: default
                val scope = str("scope") ?: "pi/s1"
                val matches = ProtocolJson.decodeFromJsonElement(ListSerializer(MatchRow.serializer()), step["matches"] ?: JsonArray(emptyList()))
                val channels = ProtocolJson.decodeFromJsonElement(ListSerializer(ChannelRow.serializer()), step["channels"] ?: JsonArray(emptyList()))
                engine.ingest(matches, null, channels, str("ingest_scope") ?: scope)
                val refuse = str("refuse")
                val context = AlertEngine.Context(
                    now = step["now"]!!.jsonPrimitive.double, live = bool("live", true), mayHold = bool("may_hold", false),
                    scope = scope, settings = settings, armed = bool("armed", true), audioProblem = str("audio_problem"),
                    mutedUntil = step["muted_until"]?.jsonPrimitive?.double ?: 0.0, age = { it.match.age }, play = { refuse },
                )
                val got = engine.flush(context).map(::actualText)
                val want = step["expect"]!!.jsonArray.map { expectText(it.jsonObject) }
                assertEquals(want, got, "scenario '$name', step ${i + 1}")
            }
        }
    }

    @Test
    fun serverMessage() {
        val m = ServerMessage.decode(File(dir, "server-message.json").readText())
        assertNotNull(m, "decodes, ignoring the unknown field")
        assertEquals("a1b2c3d4", m.boot)
        assertEquals(42, m.seq)
        val st = assertNotNull(m.state)
        assertEquals("running", st.detector.state)
        assertEquals(240, st.detector.quietAlarmWindows)
        assertEquals("b-77", st.channels[0].burstID)
        assertEquals(null, st.channels[1].strength)
        assertEquals("coherent", st.matches[0].rule)
        assertEquals("the 4G stick (usb0)", st.link?.viaText)
        assertEquals(true, st.notify?.pushover)
        assertEquals("1.6.1", m.release?.app)
        assertEquals("done", m.ops?.single()?.state)
    }
}
