package nl.slot17.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class CarMessagesTest {
    @Test fun text() {
        assertEquals("Strong burst on L154, 55 dB", CarMessages.text(154, 55.04))
        assertEquals("Strong burst on L60", CarMessages.text(60, null))
    }

    @Test fun spacedOut() {
        assertTrue(CarMessages.due(100.0, null))
        assertFalse(CarMessages.due(129.9, 100.0))
        assertTrue(CarMessages.due(130.0, 100.0))
    }

    @Test fun spokenReplies() {
        assertEquals(Sighting.Label.POLICE, CarMessages.label("Police"))
        assertEquals(Sighting.Label.POLICE, CarMessages.label("I saw the police!"))
        assertEquals(Sighting.Label.AMBULANCE, CarMessages.label("ambulance"))
        assertEquals(Sighting.Label.FIRE, CarMessages.label("Brandweer"))
        assertEquals(Sighting.Label.NOTHING, CarMessages.label("niks"))
        assertEquals(Sighting.Label.NOTHING, CarMessages.label("saw nothing"))
        assertEquals(Sighting.Label.UNKNOWN, CarMessages.label("something else"))
    }

    @Test fun notUnderstood() {
        assertNull(CarMessages.label(""))
        assertNull(CarMessages.label("okay"))
        assertNull(CarMessages.label("policeman"), "whole words only")
        assertEquals("Not understood. Say police, ambulance, fire or nothing.", CarMessages.answer(null))
        assertEquals("Marked: Police", CarMessages.answer(Sighting.Label.POLICE))
    }
}
