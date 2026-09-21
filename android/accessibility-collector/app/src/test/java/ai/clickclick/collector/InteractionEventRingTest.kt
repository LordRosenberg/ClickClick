package ai.clickclick.collector

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

class InteractionEventRingTest {
    @Test fun preservesOrderAndReportsCompleteCoverage() {
        var now = 100L
        val ring = InteractionEventRing(maxCount = 3, maxAgeMs = 1000, nowMs = { now })
        ring.append(SafeInteractionEvent(monotonicMs = now, eventType = 1, packageName = "p"))
        now += 1
        ring.append(SafeInteractionEvent(monotonicMs = now, eventType = 2, packageName = "p"))
        val read = ring.readAfter(0, 10)
        assertTrue(read.completeCoverage)
        assertEquals(listOf(1L, 2L), read.events.map { it.sequence })
    }

    @Test fun countAndAgeEvictionMakeOldCursorIncomplete() {
        var now = 100L
        val ring = InteractionEventRing(maxCount = 2, maxAgeMs = 10, nowMs = { now })
        repeat(3) { ring.append(SafeInteractionEvent(monotonicMs = now++, eventType = 1)) }
        assertFalse(ring.readAfter(0, 10).completeCoverage)
        now = 1000
        val expired = ring.readAfter(2, 10)
        assertFalse(expired.completeCoverage)
        assertTrue(expired.events.isEmpty())
    }

    @Test fun serializedRecordContainsNoContentFields() {
        // Local JVM tests use Android's stub JSONObject, so verify the DTO itself
        // cannot retain content rather than invoking serialization here.
        val fields = SafeInteractionEvent::class.java.declaredFields.map { it.name }.toSet()
        assertFalse(fields.any { it in setOf("text", "contentDescription", "hint", "value", "text_length") })
        assertTrue(fields.containsAll(setOf("editable", "password")))
    }

    @Test fun concurrentAppendsKeepUniqueOrderedSequences() {
        val ring = InteractionEventRing(maxCount = 1_000, maxAgeMs = 60_000, nowMs = { 100L })
        val pool = Executors.newFixedThreadPool(8)
        repeat(800) { value ->
            pool.execute {
                ring.append(SafeInteractionEvent(monotonicMs = 100, eventType = value))
            }
        }
        pool.shutdown()
        assertTrue(pool.awaitTermination(3, TimeUnit.SECONDS))

        val events = ring.readAfter(0, InteractionEventRing.MAX_READ_LIMIT).events
        assertEquals(InteractionEventRing.MAX_READ_LIMIT, events.size)
        assertEquals(events.map { it.sequence }.distinct().size, events.size)
        assertEquals(events.map { it.sequence }.sorted(), events.map { it.sequence })
        assertEquals(800L, ring.currentSequence())
    }
}
