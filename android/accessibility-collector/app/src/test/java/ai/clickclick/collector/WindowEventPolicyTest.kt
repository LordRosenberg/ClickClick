package ai.clickclick.collector

import android.view.accessibility.AccessibilityEvent as Event
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class WindowEventPolicyTest {
    private fun windows(changes: Int?, windowId: Int = 7) =
        WindowEventPolicy.invalidatesSnapshot(Event.TYPE_WINDOWS_CHANGED, windowId, changes, 0)

    private fun state(changes: Int, windowId: Int = 7) =
        WindowEventPolicy.invalidatesSnapshot(Event.TYPE_WINDOW_STATE_CHANGED, windowId, null, changes)

    @Test fun pureWindowMetadataDoesNotInvalidate() {
        assertFalse(windows(Event.WINDOWS_CHANGE_TITLE))
        assertFalse(windows(Event.WINDOWS_CHANGE_ACCESSIBILITY_FOCUSED))
        assertFalse(windows(Event.WINDOWS_CHANGE_TITLE or Event.WINDOWS_CHANGE_ACCESSIBILITY_FOCUSED))
    }

    @Test fun geometryTopologyOwnershipAndInputFocusStillInvalidate() {
        for (change in listOf(
            Event.WINDOWS_CHANGE_ADDED, Event.WINDOWS_CHANGE_REMOVED,
            Event.WINDOWS_CHANGE_BOUNDS, Event.WINDOWS_CHANGE_LAYER,
            Event.WINDOWS_CHANGE_ACTIVE, Event.WINDOWS_CHANGE_FOCUSED,
            Event.WINDOWS_CHANGE_PARENT, Event.WINDOWS_CHANGE_CHILDREN,
            Event.WINDOWS_CHANGE_PIP,
        )) {
            assertTrue("change=$change", windows(change))
            assertTrue("mixed title + change=$change", windows(change or Event.WINDOWS_CHANGE_TITLE))
            assertTrue("mixed a11y focus + change=$change", windows(change or Event.WINDOWS_CHANGE_ACCESSIBILITY_FOCUSED))
        }
    }

    @Test fun absentAndFutureWindowTypesRemainConservative() {
        assertTrue(windows(null)) // API 26/27 cannot report windowChanges.
        assertTrue(windows(0))
        assertTrue(windows(1 shl 29))
        assertTrue(windows(Event.WINDOWS_CHANGE_TITLE or (1 shl 29)))
        assertTrue(windows(Event.WINDOWS_CHANGE_TITLE, windowId = -1))
    }

    @Test fun paneTitleAloneDoesNotInvalidate() {
        assertFalse(state(Event.CONTENT_CHANGE_TYPE_PANE_TITLE))
        assertTrue(state(Event.CONTENT_CHANGE_TYPE_PANE_TITLE, windowId = -1))
    }

    @Test fun paneAppearanceDisappearanceAndUnknownStateStillInvalidate() {
        for (change in listOf(
            0, Event.CONTENT_CHANGE_TYPE_PANE_APPEARED,
            Event.CONTENT_CHANGE_TYPE_PANE_DISAPPEARED,
            Event.CONTENT_CHANGE_TYPE_SUBTREE, Event.CONTENT_CHANGE_TYPE_TEXT,
            1 shl 29,
        )) {
            assertTrue("state=$change", state(change))
            if (change != 0) {
                assertTrue("mixed title + state=$change", state(change or Event.CONTENT_CHANGE_TYPE_PANE_TITLE))
            }
        }
    }

    @Test fun ordinaryContentAndInteractionEventsDoNotResetWindowQuietTime() {
        for (type in listOf(
            Event.TYPE_WINDOW_CONTENT_CHANGED, Event.TYPE_VIEW_TEXT_CHANGED,
            Event.TYPE_VIEW_CLICKED, Event.TYPE_VIEW_SCROLLED,
        )) {
            assertFalse(WindowEventPolicy.invalidatesSnapshot(type, -1, null, 0))
        }
    }
}
