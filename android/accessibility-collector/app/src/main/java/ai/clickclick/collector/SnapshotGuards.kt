package ai.clickclick.collector

import java.util.concurrent.locks.ReentrantLock

class SnapshotAdmission {
    private val lock = ReentrantLock()

    fun <T> capture(busy: () -> T, snapshot: () -> T): T {
        if (!lock.tryLock()) return busy()
        return try { snapshot() } finally { lock.unlock() }
    }
}

object SnapshotRequestPolicy {
    fun rejection(
        version: Int,
        token: String,
        expectedToken: String,
        operation: String,
    ): String? = when {
        version != SnapshotProtocol.VERSION -> "unsupported_version"
        token != expectedToken -> "authentication_failed"
        operation !in setOf(
            "health",
            "snapshot",
            "node_click",
            "events_cursor",
            "events_after_sequence",
            "power_lease_acquire",
            "power_lease_renew",
            "power_lease_release",
            "power_lease_status",
        ) -> "unsupported_operation"
        else -> null
    }
}

object InteractionEventRequestPolicy {
    fun rejection(afterSequence: Long, limit: Int): String? = when {
        afterSequence < 0 -> "invalid_after_sequence"
        limit !in 1..InteractionEventRing.MAX_READ_LIMIT -> "invalid_event_limit"
        else -> null
    }
}

data class FencedCapture<T>(
    val value: T,
    val generationBefore: Long,
    val generationAfter: Long,
    val stable: Boolean,
    val attempts: Int,
)

object SnapshotGenerationFence {
    fun <T> capture(generation: () -> Long, snapshot: () -> T): FencedCapture<T> {
        // The host owns the bounded settle/retry. Never double a potentially
        // slow Binder traversal inside an already budgeted request.
        val before = generation()
        val value = snapshot()
        val after = generation()
        return FencedCapture(value, before, after, before == after, 1)
    }
}
