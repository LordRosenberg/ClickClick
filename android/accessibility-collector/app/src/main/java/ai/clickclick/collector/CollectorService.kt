package ai.clickclick.collector

import android.accessibilityservice.AccessibilityService
import android.graphics.Rect
import android.os.SystemClock
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicLong

class CollectorService : AccessibilityService() {
    companion object {
        @Volatile var instance: CollectorService? = null
        private val generation = AtomicLong(0)
        private val windowGeneration = AtomicLong(0)
        private val lastWindowChangeMs = AtomicLong(0)
        private val filteredWindowEvents = AtomicLong(0)
        const val MAX_NODES = 4000
        const val MAX_DEPTH = 60
        // API 33 limits each prefetch batch to 50 nodes. Return the requested
        // child and its batch together so a busy app cannot repeatedly strand
        // us waiting for the separately delivered, interruptible prefetch.
        private const val CHILD_PREFETCH_STRATEGY =
            AccessibilityNodeInfo.FLAG_PREFETCH_ANCESTORS or
                AccessibilityNodeInfo.FLAG_PREFETCH_SIBLINGS or
                AccessibilityNodeInfo.FLAG_PREFETCH_DESCENDANTS_DEPTH_FIRST or
                AccessibilityNodeInfo.FLAG_PREFETCH_UNINTERRUPTIBLE
        val INTERACTION_EVENT_TYPES = setOf(
            AccessibilityEvent.TYPE_VIEW_CLICKED,
            AccessibilityEvent.TYPE_VIEW_LONG_CLICKED,
            AccessibilityEvent.TYPE_VIEW_SCROLLED,
            AccessibilityEvent.TYPE_VIEW_FOCUSED,
            AccessibilityEvent.TYPE_VIEW_SELECTED,
        )
    }

    private var snapshotServer: SnapshotSocketServer? = null
    private var screenBrightLease: ScreenBrightLeaseController? = null
    private val interactionEvents = InteractionEventRing(nowMs = SystemClock::elapsedRealtime)
    private val snapshotAdmission = SnapshotAdmission()
    private val nodeClicks = NodeClickStore()
    private val cleanupHandler = Handler(Looper.getMainLooper())
    private val pruneNodes = object : Runnable {
        override fun run() {
            nodeClicks.prune()
            cleanupHandler.postDelayed(this, 20_000)
        }
    }

    override fun onServiceConnected() {
        snapshotServer?.stop()
        screenBrightLease?.close()
        cleanupHandler.removeCallbacks(pruneNodes)
        nodeClicks.clear()
        cleanupHandler.postDelayed(pruneNodes, 20_000)
        instance = this
        generation.incrementAndGet()
        windowGeneration.incrementAndGet()
        lastWindowChangeMs.set(SystemClock.elapsedRealtime())
        screenBrightLease = ScreenBrightLeaseController(this)
        snapshotServer = SnapshotSocketServer(
            ::snapshot,
            ::health,
            { request -> screenBrightLease?.handle(request) ?: JSONObject()
                .put("lease_state", "error")
                .put("error_detail", "controller_unavailable") },
            interactionEvents::currentSequence,
            { after, limit -> interactionEvents.readAfter(after, limit).toJson() },
            { request -> nodeClicks.click(request.optString("node_handle")) },
        ).also { it.start() }
    }

    override fun onDestroy() {
        cleanupHandler.removeCallbacks(pruneNodes)
        nodeClicks.clear()
        snapshotServer?.stop()
        snapshotServer = null
        screenBrightLease?.close()
        screenBrightLease = null
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onInterrupt() = Unit

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        generation.incrementAndGet()
        if (event?.eventType == AccessibilityEvent.TYPE_WINDOWS_CHANGED ||
            event?.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            val invalidates = WindowEventPolicy.invalidatesSnapshot(
                eventType = event.eventType,
                windowId = event.windowId,
                windowChanges = if (Build.VERSION.SDK_INT >= 28 &&
                    event.eventType == AccessibilityEvent.TYPE_WINDOWS_CHANGED) event.windowChanges else null,
                contentChanges = event.contentChangeTypes,
            )
            if (invalidates) {
                windowGeneration.incrementAndGet()
                lastWindowChangeMs.set(SystemClock.elapsedRealtime())
            } else {
                filteredWindowEvents.incrementAndGet()
            }
        }
        if (event == null || event.eventType !in INTERACTION_EVENT_TYPES) return
        val source = event.source
        val bounds = Rect()
        source?.getBoundsInScreen(bounds)
        val password = source?.isPassword == true
        interactionEvents.append(SafeInteractionEvent(
            monotonicMs = SystemClock.elapsedRealtime(),
            eventType = event.eventType,
            packageName = event.packageName?.toString().orEmpty(),
            windowId = event.windowId,
            sourceClass = source?.className?.toString().orEmpty(),
            resourceId = source?.viewIdResourceName.orEmpty(),
            bounds = if (source == null) emptyList() else listOf(
                bounds.left, bounds.top, bounds.right, bounds.bottom,
            ),
            clickable = source?.isClickable == true,
            longClickable = source?.isLongClickable == true,
            scrollable = source?.isScrollable == true,
            editable = source?.isEditable == true,
            password = password,
        ))
    }

    fun health(): JSONObject = JSONObject()
        .put("ready", true)
        .put("generation", generation.get())
        .put("window_generation", windowGeneration.get())
        .put("filtered_window_events", filteredWindowEvents.get())
        .put("window_quiet_ms", SystemClock.elapsedRealtime() - lastWindowChangeMs.get())
        .put("service", "connected")
        .put("protocol_version", SnapshotProtocol.VERSION)
        .put("capabilities", JSONArray(listOf("events_after_sequence_v1", "node_click_v1")))
        .put("event_sequence", interactionEvents.currentSequence())
        .put("cache_enabled", if (Build.VERSION.SDK_INT >= 33) isCacheEnabled else JSONObject.NULL)
        .put("socket_name", snapshotServer?.socketName.orEmpty())
        .put("token", snapshotServer?.token.orEmpty())
        .put("screen_bright_lease", screenBrightLease?.status() ?: JSONObject()
            .put("active", false)
            .put("held", false))

    fun snapshot(): JSONObject = snapshotAdmission.capture(
        busy = { JSONObject().put("schema_version", 1)
            .put("generation", generation.get())
            .put("window_generation", windowGeneration.get())
            .put("window_quiet_ms", SystemClock.elapsedRealtime() - lastWindowChangeMs.get())
            .put("captured_monotonic_ms", SystemClock.elapsedRealtime().toDouble())
            .put("complete", false).put("reasons", JSONArray(listOf("snapshot_busy")))
            .put("windows", JSONArray()) },
        snapshot = ::captureSnapshot,
    )

    private fun captureSnapshot(): JSONObject {
        val started = SystemClock.elapsedRealtime()
        val contentBefore = generation.get()
        val fenced = SnapshotGenerationFence.capture(windowGeneration::get, ::snapshotOnce)
        val result = fenced.value
            .put("generation", generation.get())
            .put("generation_before", contentBefore)
            .put("generation_after", generation.get())
            .put("window_generation", fenced.generationAfter)
            .put("capture_attempts", fenced.attempts)
            .put("snapshot_elapsed_ms", SystemClock.elapsedRealtime() - started)
            .put("content_changed_during_capture", contentBefore != generation.get())
            .put("window_quiet_ms", SystemClock.elapsedRealtime() - lastWindowChangeMs.get())
        if (!fenced.stable) {
            result.put("complete", false)
            result.getJSONArray("reasons").put("generation_changed")
        }
        return result
    }

    private fun snapshotOnce(): JSONObject {
        val started = SystemClock.elapsedRealtime()
        val reasons = JSONArray()
        val records = JSONArray()
        var complete = true
        // A window transition may leave cached screen bounds after its animation.
        // Invalidate once per snapshot; traversal can still share its own cache.
        val cacheCleared = if (Build.VERSION.SDK_INT >= 33) clearCache() else null
        if (cacheCleared == false) {
            complete = false
            reasons.put("cache_clear_failed")
        }
        var remaining = MAX_NODES
        val current = windows
            .filter { Build.VERSION.SDK_INT < 30 || it.displayId == 0 }
            .sortedByDescending { it.layer }
        val windowsReady = SystemClock.elapsedRealtime()

        for (window in current) {
            val record = windowRecord(window)
            val rootStarted = SystemClock.elapsedRealtime()
            // Unlike window.getRoot(flags), the active-root API actually
            // supports prefetch on Android 13. The cache was just cleared.
            // Validate identity because the active window can change between
            // the windows query and this root query (e.g. an opening popup).
            val root = if (Build.VERSION.SDK_INT >= 33 && window.isActive) {
                val activeRoot = getRootInActiveWindow(CHILD_PREFETCH_STRATEGY)
                if (activeRoot?.windowId == window.id) activeRoot else window.root
            } else {
                window.root ?: if (window.isActive) {
                    rootInActiveWindow?.takeIf { it.windowId == window.id }
                } else null
            }
            record.put("root_elapsed_ms", SystemClock.elapsedRealtime() - rootStarted)
            val traversalStarted = SystemClock.elapsedRealtimeNanos()
            if (root == null) {
                complete = false
                reasons.put("window_${window.id}_missing_root")
                record.put("complete", false).put("reason", "missing_root")
            } else {
                val state = TraversalState(remaining, deadlineMs = started + 2200)
                record.put("root", nodeRecord(root, 0, state))
                record.put("child_fetch_ms", state.childFetchMs)
                record.put("child_fetch_count", state.childFetchCount)
                record.put("slowest_child_fetch_ms", state.slowestChildFetchMs)
                remaining = state.remaining
                if (!state.complete) {
                    complete = false
                    reasons.put("window_${window.id}_${state.reason}")
                    record.put("complete", false).put("reason", state.reason)
                }
            }
            record.put(
                "traversal_elapsed_ms",
                (SystemClock.elapsedRealtimeNanos() - traversalStarted) / 1_000_000.0,
            )
            records.put(record)
        }
        if (current.isEmpty()) {
            complete = false
            reasons.put("no_interactive_windows")
        }
        return JSONObject()
            .put("schema_version", 1)
            .put("windows_elapsed_ms", windowsReady - started)
            .put("captured_monotonic_ms", SystemClock.elapsedRealtime().toDouble())
            .put("cache_cleared", cacheCleared ?: JSONObject.NULL)
            .put("child_prefetch_strategy", if (Build.VERSION.SDK_INT >= 33)
                CHILD_PREFETCH_STRATEGY else JSONObject.NULL)
            .put("complete", complete)
            .put("reasons", reasons)
            .put("windows", records)
    }

    private fun windowRecord(window: AccessibilityWindowInfo): JSONObject {
        val bounds = Rect()
        window.getBoundsInScreen(bounds)
        return JSONObject()
            .put("window_id", window.id)
            .put("type", window.type)
            .put("layer", window.layer)
            .put("bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)))
            .put("active", window.isActive)
            .put("focused", window.isFocused)
            .put("display_id", if (Build.VERSION.SDK_INT >= 30) window.displayId else 0)
            .put("complete", true)
    }

    private fun nodeRecord(
        node: AccessibilityNodeInfo,
        depth: Int,
        state: TraversalState,
    ): JSONObject {
        if (depth > MAX_DEPTH || state.remaining <= 0 || SystemClock.elapsedRealtime() >= state.deadlineMs) {
            state.complete = false
            state.reason = when {
                SystemClock.elapsedRealtime() >= state.deadlineMs -> "traversal_budget"
                depth > MAX_DEPTH -> "depth_guard"
                else -> "node_guard"
            }
            return JSONObject().put("class", "Truncated")
        }
        state.remaining--
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        val children = JSONArray()
        for (index in 0 until node.childCount) {
            if (SystemClock.elapsedRealtime() >= state.deadlineMs) {
                state.complete = false
                state.reason = "traversal_budget"
                break
            }
            val fetchStarted = SystemClock.elapsedRealtime()
            // Keep the once-per-snapshot cache invalidation above. Changing
            // window.getRoot(flags) would not enable this batching: Android 13
            // bypasses the cache for that call and removes its prefetch flags.
            val child = if (Build.VERSION.SDK_INT >= 33) {
                node.getChild(index, CHILD_PREFETCH_STRATEGY)
            } else {
                node.getChild(index)
            }
            val fetchMs = SystemClock.elapsedRealtime() - fetchStarted
            state.childFetchMs += fetchMs
            state.childFetchCount++
            state.slowestChildFetchMs = maxOf(state.slowestChildFetchMs, fetchMs)
            if (child == null) continue
            if (child.isVisibleToUser) children.put(nodeRecord(child, depth + 1, state))
        }
        return JSONObject()
            .put("class", node.className?.toString().orEmpty())
            .put("package", node.packageName?.toString().orEmpty())
            .put("text", node.text?.toString().orEmpty())
            .put("contentDescription", node.contentDescription?.toString().orEmpty())
            .put("hint", node.hintText?.toString().orEmpty())
            .put("resource_id", node.viewIdResourceName.orEmpty())
            .put("node_handle", nodeClicks.retain(node))
            .put("bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)))
            .put("clickable", node.isClickable)
            .put("checkable", node.isCheckable)
            .put("editable", node.isEditable)
            .put("enabled", node.isEnabled)
            .put("focused", node.isFocused)
            .put("focusable", node.isFocusable)
            .put("checked", node.isChecked)
            .put("selected", node.isSelected)
            .put("long_clickable", node.isLongClickable)
            .put("scrollable", node.isScrollable)
            .put("password", node.isPassword)
            .put("children", children)
    }

    private data class TraversalState(
        var remaining: Int,
        val deadlineMs: Long,
        var complete: Boolean = true,
        var reason: String = "",
        var childFetchMs: Long = 0,
        var childFetchCount: Int = 0,
        var slowestChildFetchMs: Long = 0,
    )
}
