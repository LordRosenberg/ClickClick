package ai.clickclick.collector

import android.graphics.Rect
import android.os.SystemClock
import android.util.Base64
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest
import java.util.UUID

/** Observation-scoped, single-use references, never selectors into a new tree. */
class NodeClickStore {
    private data class Entry(
        val node: AccessibilityNodeInfo,
        val identity: String,
        val createdMs: Long,
    )
    private val entries = LinkedHashMap<String, Entry>()

    @Synchronized
    fun retain(node: AccessibilityNodeInfo): String {
        if (!supportsClick(node)) return ""
        prune()
        while (entries.size >= MAX_ENTRIES) removeOldest()
        val handle = UUID.randomUUID().toString()
        @Suppress("DEPRECATION")
        val copy = AccessibilityNodeInfo.obtain(node)
        entries[handle] = Entry(copy, identity(node), SystemClock.elapsedRealtime())
        return handle
    }

    @Synchronized
    fun prune() {
        val cutoff = SystemClock.elapsedRealtime() - TTL_MS
        while (entries.isNotEmpty() && entries.values.first().createdMs < cutoff) removeOldest()
    }

    @Synchronized
    fun clear() {
        while (entries.isNotEmpty()) removeOldest()
    }

    @Suppress("DEPRECATION")
    private fun removeOldest() {
        val iterator = entries.entries.iterator()
        if (iterator.hasNext()) {
            iterator.next().value.node.recycle()
            iterator.remove()
        }
    }

    fun click(handle: String): JSONObject {
        val started = SystemClock.elapsedRealtime()
        // Consume before any Binder call. Disconnects/retries cannot replay this click.
        val entry = synchronized(this) {
            prune()
            entries.remove(handle)
        } ?: return JSONObject().put("node_click_status", "expired_or_consumed")
            .put("action_attempted", false).put("performed", JSONObject.NULL)
        var attempted = false
        fun result(status: String, performed: Boolean? = null): JSONObject = JSONObject()
            .put("node_click_status", status)
            .put("action_attempted", attempted)
            .put("performed", performed ?: JSONObject.NULL)
            .put("elapsed_ms", SystemClock.elapsedRealtime() - started)
        try {
            val node = entry.node
            // Exactly one targeted refresh: no root query, traversal, or retry loop.
            if (!node.refresh()) return result("obsolete")
            if (entry.identity != identity(node)) return result("identity_changed")
            if (!node.isVisibleToUser || !node.isEnabled) return result("not_available")
            if (!supportsClick(node)) return result("no_longer_clickable")
            // A stalled refresh must not dispatch a late click after the host timeout.
            if (SystemClock.elapsedRealtime() - started > 1500) return result("refresh_failed")
            val bounds = Rect()
            node.getBoundsInScreen(bounds)
            attempted = true
            val performed = node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            return result(if (performed) "performed" else "not_performed", performed)
                .put("bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)))
        } catch (_: Exception) {
            // Never turn an uncertain action result into a coordinate retry.
            return result(if (attempted) "outcome_unknown" else "refresh_failed")
        } finally {
            @Suppress("DEPRECATION")
            entry.node.recycle()
        }
    }

    private fun supportsClick(node: AccessibilityNodeInfo): Boolean =
        node.isClickable && node.actionList.any { it.id == AccessibilityNodeInfo.ACTION_CLICK }

    private fun identity(node: AccessibilityNodeInfo): String {
        // Hash locally; no extra text or editable/password values in action responses.
        // Bounds/focus/selection are deliberately absent: movement is permitted.
        val fields = listOf(
            node.windowId.toString(), node.packageName?.toString().orEmpty(),
            node.className?.toString().orEmpty(), node.viewIdResourceName.orEmpty(),
            if (node.isPassword) "" else node.text?.toString().orEmpty(),
            node.contentDescription?.toString().orEmpty(), node.hintText?.toString().orEmpty(),
            node.isPassword.toString(), node.isEditable.toString(), node.isCheckable.toString(),
        )
        val encoded = fields.joinToString("") { "${it.length}:$it" }.toByteArray(Charsets.UTF_8)
        return Base64.encodeToString(MessageDigest.getInstance("SHA-256").digest(encoded), Base64.NO_WRAP)
    }

    companion object {
        private const val TTL_MS = 120_000L
        private const val MAX_ENTRIES = 16_000
    }
}
