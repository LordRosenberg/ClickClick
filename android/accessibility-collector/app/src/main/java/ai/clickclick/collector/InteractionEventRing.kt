package ai.clickclick.collector

import org.json.JSONArray
import org.json.JSONObject
import java.util.ArrayDeque

data class SafeInteractionEvent(
    val sequence: Long = 0,
    val monotonicMs: Long,
    val eventType: Int,
    val packageName: String = "",
    val windowId: Int = -1,
    val sourceClass: String = "",
    val resourceId: String = "",
    val bounds: List<Int> = emptyList(),
    val clickable: Boolean = false,
    val longClickable: Boolean = false,
    val scrollable: Boolean = false,
    val editable: Boolean = false,
    val password: Boolean = false,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("sequence", sequence)
        .put("monotonic_ms", monotonicMs)
        .put("event_type", eventType)
        .put("package", packageName)
        .put("window_id", windowId)
        .put("source_class", sourceClass)
        .put("resource_id", resourceId)
        .put("bounds", JSONArray(bounds))
        .put("clickable", clickable)
        .put("long_clickable", longClickable)
        .put("scrollable", scrollable)
        .put("editable", editable)
        .put("password", password)
}

data class InteractionEventRead(
    val events: List<SafeInteractionEvent>,
    val oldestSequence: Long,
    val currentSequence: Long,
    val completeCoverage: Boolean,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("schema_version", 1)
        .put("oldest_sequence", oldestSequence)
        .put("current_sequence", currentSequence)
        .put("complete_coverage", completeCoverage)
        .put("events", JSONArray().also { output -> events.forEach { output.put(it.toJson()) } })
}

/** Small in-memory metadata ring. Android AccessibilityEvent objects never enter it. */
class InteractionEventRing(
    private val maxCount: Int = DEFAULT_MAX_COUNT,
    private val maxAgeMs: Long = DEFAULT_MAX_AGE_MS,
    private val nowMs: () -> Long,
) {
    private val lock = Any()
    private val events = ArrayDeque<SafeInteractionEvent>()
    private var currentSequence = 0L

    fun currentSequence(): Long = synchronized(lock) { currentSequence }

    fun append(event: SafeInteractionEvent): Long = synchronized(lock) {
        currentSequence += 1
        val stored = event.copy(sequence = currentSequence)
        events.addLast(stored)
        evictLocked(nowMs())
        currentSequence
    }

    fun readAfter(afterSequence: Long, limit: Int): InteractionEventRead = synchronized(lock) {
        evictLocked(nowMs())
        val oldest = events.firstOrNull()?.sequence ?: (currentSequence + 1)
        val complete = afterSequence >= oldest - 1
        val rows = events.asSequence()
            .filter { it.sequence > afterSequence }
            .take(limit.coerceIn(1, MAX_READ_LIMIT))
            .toList()
        InteractionEventRead(rows, oldest, currentSequence, complete)
    }

    private fun evictLocked(now: Long) {
        while (events.size > maxCount) events.removeFirst()
        while (events.isNotEmpty() && now - events.first().monotonicMs > maxAgeMs) {
            events.removeFirst()
        }
    }

    companion object {
        const val DEFAULT_MAX_COUNT = 256
        const val DEFAULT_MAX_AGE_MS = 15_000L
        const val MAX_READ_LIMIT = 128
    }
}
