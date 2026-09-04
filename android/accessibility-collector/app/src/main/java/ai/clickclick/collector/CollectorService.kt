package ai.clickclick.collector

import android.accessibilityservice.AccessibilityService
import android.graphics.Rect
import android.os.SystemClock
import android.os.Build
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
        const val MAX_NODES = 4000
        const val MAX_DEPTH = 60
    }

    private var snapshotServer: SnapshotSocketServer? = null

    override fun onServiceConnected() {
        instance = this
        generation.incrementAndGet()
        snapshotServer = SnapshotSocketServer(::snapshot, ::health).also { it.start() }
    }

    override fun onDestroy() {
        snapshotServer?.stop()
        snapshotServer = null
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onInterrupt() = Unit

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Events only invalidate the generation. Event text/source is never retained.
        generation.incrementAndGet()
    }

    fun health(): JSONObject = JSONObject()
        .put("ready", true)
        .put("generation", generation.get())
        .put("service", "connected")
        .put("protocol_version", SnapshotProtocol.VERSION)
        .put("socket_name", snapshotServer?.socketName.orEmpty())
        .put("token", snapshotServer?.token.orEmpty())

    fun snapshot(): JSONObject {
        val fenced = SnapshotGenerationFence.capture(generation::get, ::snapshotOnce)
        val result = fenced.value
            .put("generation", fenced.generationAfter)
            .put("generation_before", fenced.generationBefore)
            .put("generation_after", fenced.generationAfter)
        if (!fenced.stable) {
            result.put("complete", false)
            result.getJSONArray("reasons").put("generation_changed")
        }
        return result
    }

    private fun snapshotOnce(): JSONObject {
        val reasons = JSONArray()
        val records = JSONArray()
        var complete = true
        var remaining = MAX_NODES
        val current = windows
            .filter { Build.VERSION.SDK_INT < 30 || it.displayId == 0 }
            .sortedByDescending { it.layer }

        for (window in current) {
            val record = windowRecord(window)
            // Some OEMs expose the active window record but return a null
            // window root. rootInActiveWindow is still a current platform
            // snapshot and is safe only for that active window.
            val root = window.root ?: if (window.isActive) rootInActiveWindow else null
            val traversalStarted = SystemClock.elapsedRealtimeNanos()
            if (root == null) {
                complete = false
                reasons.put("window_${window.id}_missing_root")
                record.put("complete", false).put("reason", "missing_root")
            } else {
                val state = TraversalState(remaining)
                record.put("root", nodeRecord(root, 0, state))
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
            .put("captured_monotonic_ms", SystemClock.elapsedRealtime().toDouble())
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
        if (depth > MAX_DEPTH || state.remaining <= 0) {
            state.complete = false
            state.reason = if (depth > MAX_DEPTH) "depth_guard" else "node_guard"
            return JSONObject().put("class", "Truncated")
        }
        state.remaining--
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        val children = JSONArray()
        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            if (child.isVisibleToUser) children.put(nodeRecord(child, depth + 1, state))
        }
        return JSONObject()
            .put("class", node.className?.toString().orEmpty())
            .put("package", node.packageName?.toString().orEmpty())
            .put("text", node.text?.toString().orEmpty())
            .put("contentDescription", node.contentDescription?.toString().orEmpty())
            .put("hint", node.hintText?.toString().orEmpty())
            .put("resource_id", node.viewIdResourceName.orEmpty())
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
        var complete: Boolean = true,
        var reason: String = "",
    )
}
