package ai.clickclick.collector

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SnapshotGuardsTest {
    @Test fun requestPolicyRejectsWrongVersionTokenAndOperation() {
        assertEquals(
            "unsupported_version",
            SnapshotRequestPolicy.rejection(2, "secret", "secret", "health"),
        )
        assertEquals(
            "authentication_failed",
            SnapshotRequestPolicy.rejection(1, "wrong", "secret", "health"),
        )
        assertEquals(
            "unsupported_operation",
            SnapshotRequestPolicy.rejection(1, "secret", "secret", "delete"),
        )
        assertNull(SnapshotRequestPolicy.rejection(1, "secret", "secret", "snapshot"))
    }

    @Test fun generationFenceRetriesOnceAndMarksStable() {
        val generations = ArrayDeque(listOf(1L, 2L, 3L, 3L))
        var captures = 0
        val result = SnapshotGenerationFence.capture(
            generation = { generations.removeFirst() },
            snapshot = { ++captures },
        )
        assertTrue(result.stable)
        assertEquals(2, result.attempts)
        assertEquals(2, captures)
    }

    @Test fun generationFenceStopsAfterOneRetry() {
        val generations = ArrayDeque(listOf(1L, 2L, 3L, 4L))
        val result = SnapshotGenerationFence.capture(
            generation = { generations.removeFirst() },
            snapshot = { "tree" },
        )
        assertFalse(result.stable)
        assertEquals(2, result.attempts)
        assertEquals(3L, result.generationBefore)
        assertEquals(4L, result.generationAfter)
    }
}
