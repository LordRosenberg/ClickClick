package ai.clickclick.collector

object SnapshotRequestPolicy {
    fun rejection(
        version: Int,
        token: String,
        expectedToken: String,
        operation: String,
    ): String? = when {
        version != SnapshotProtocol.VERSION -> "unsupported_version"
        token != expectedToken -> "authentication_failed"
        operation !in setOf("health", "snapshot") -> "unsupported_operation"
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
        repeat(2) { index ->
            val before = generation()
            val value = snapshot()
            val after = generation()
            if (before == after || index == 1) {
                return FencedCapture(value, before, after, before == after, index + 1)
            }
        }
        error("unreachable")
    }
}
