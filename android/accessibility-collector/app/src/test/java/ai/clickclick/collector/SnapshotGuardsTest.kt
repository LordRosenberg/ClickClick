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
        assertNull(SnapshotRequestPolicy.rejection(1, "secret", "secret", "events_cursor"))
        assertNull(SnapshotRequestPolicy.rejection(1, "secret", "secret", "events_after_sequence"))
        assertEquals("invalid_after_sequence", InteractionEventRequestPolicy.rejection(-1, 1))
        assertEquals("invalid_event_limit", InteractionEventRequestPolicy.rejection(0, 129))
        assertNull(
            SnapshotRequestPolicy.rejection(
                1, "secret", "secret", "power_lease_acquire",
            ),
        )
        assertNull(
            SnapshotRequestPolicy.rejection(
                1, "secret", "secret", "power_lease_release",
            ),
        )
    }

    @Test fun screenLeasePolicyRejectsUnsafeBounds() {
        assertEquals("missing_lease_id", ScreenBrightLeasePolicy.rejection("", 30_000))
        assertEquals("ttl_out_of_range", ScreenBrightLeasePolicy.rejection("task", 1_000))
        assertEquals("ttl_out_of_range", ScreenBrightLeasePolicy.rejection("task", 121_000))
        assertNull(ScreenBrightLeasePolicy.rejection("task", 90_000))
    }

    @Test fun generationFenceAcceptsUnchangedWindow() {
        val generations = ArrayDeque(listOf(3L, 3L))
        var captures = 0
        val result = SnapshotGenerationFence.capture(
            generation = { generations.removeFirst() },
            snapshot = { ++captures },
        )
        assertTrue(result.stable)
        assertEquals(1, result.attempts)
        assertEquals(1, captures)
    }

    @Test fun generationFenceLeavesRetryToHost() {
        val generations = ArrayDeque(listOf(1L, 2L, 3L, 4L))
        val result = SnapshotGenerationFence.capture(
            generation = { generations.removeFirst() },
            snapshot = { "tree" },
        )
        assertFalse(result.stable)
        assertEquals(1, result.attempts)
        assertEquals(1L, result.generationBefore)
        assertEquals(2L, result.generationAfter)
    }

    @Test fun concurrentSnapshotDoesNotQueueOrClearAnotherSnapshotsCache() {
        val gate = SnapshotAdmission()
        val entered = java.util.concurrent.CountDownLatch(1)
        val release = java.util.concurrent.CountDownLatch(1)
        val thread = Thread {
            gate.capture({ error("unexpected busy") }) { entered.countDown(); release.await(); "done" }
        }
        thread.start()
        assertTrue(entered.await(2, java.util.concurrent.TimeUnit.SECONDS))
        try {
            assertEquals("busy", gate.capture({ "busy" }) { error("must not traverse") })
        } finally {
            release.countDown()
            thread.join(2000)
        }
        assertEquals("next", gate.capture({ "busy" }) { "next" })
    }
}
