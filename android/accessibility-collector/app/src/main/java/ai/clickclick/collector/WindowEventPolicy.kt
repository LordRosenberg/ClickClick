package ai.clickclick.collector

import android.view.accessibility.AccessibilityEvent

/** Narrow metadata-only exemptions; unknown or mixed changes remain fenced. */
object WindowEventPolicy {
    private const val WINDOW_METADATA = AccessibilityEvent.WINDOWS_CHANGE_TITLE or
        AccessibilityEvent.WINDOWS_CHANGE_ACCESSIBILITY_FOCUSED

    fun invalidatesSnapshot(
        eventType: Int,
        windowId: Int,
        windowChanges: Int?,
        contentChanges: Int,
    ): Boolean = when (eventType) {
        AccessibilityEvent.TYPE_WINDOWS_CHANGED ->
            windowId < 0 || windowChanges == null || windowChanges == 0 ||
                (windowChanges and WINDOW_METADATA.inv()) != 0
        AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED ->
            windowId < 0 || contentChanges != AccessibilityEvent.CONTENT_CHANGE_TYPE_PANE_TITLE
        else -> false
    }
}
