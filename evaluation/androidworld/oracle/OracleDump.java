import android.app.UiAutomation;
import android.graphics.Point;
import android.graphics.Rect;
import android.os.HandlerThread;
import android.os.Looper;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.util.List;
import java.util.ArrayList;
import java.util.Collections;
import android.accessibilityservice.AccessibilityServiceInfo;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import org.json.JSONArray;
import org.json.JSONObject;

/** Independent API 33 oracle read. Deliberately does not wait for content idle. */
public final class OracleDump {
    private static JSONObject rectangle(Rect b) throws Exception {
        return new JSONObject().put("left", b.left).put("right", b.right)
            .put("top", b.top).put("bottom", b.bottom);
    }
    private static String windowsIdentity(UiAutomation automation) {
        List<String> keys = new ArrayList<>();
        for (AccessibilityWindowInfo w : automation.getWindows()) {
            Rect b = new Rect(); w.getBoundsInScreen(b);
            keys.add(w.getId() + ":" + w.getType() + ":" + w.getLayer() + ":" + b);
            w.recycle();
        }
        Collections.sort(keys);
        return keys.toString();
    }
    private static JSONArray collectWindows(UiAutomation automation) throws Exception {
        JSONArray windows = new JSONArray();
        List<AccessibilityWindowInfo> available = automation.getWindows();
        if (available.isEmpty()) throw new IllegalStateException("No oracle windows");
        try {
            for (AccessibilityWindowInfo w : available) {
                AccessibilityNodeInfo node = w.getRoot();
                if (node == null) throw new IllegalStateException("Missing oracle window root: " + w.getId());
                JSONArray nodes = new JSONArray();
                try { appendNode(node, nodes, 0); } finally { node.recycle(); }
                Rect b = new Rect(); w.getBoundsInScreen(b);
                windows.put(new JSONObject().put("id", w.getId()).put("layer", w.getLayer())
                    .put("bounds_in_screen", rectangle(b)).put("title", string(w.getTitle()))
                    .put("is_active", w.isActive()).put("is_focused", w.isFocused())
                    .put("is_accessibility_focused", w.isAccessibilityFocused())
                    .put("tree", new JSONObject().put("nodes", nodes)));
            }
        } finally { for (AccessibilityWindowInfo w : available) w.recycle(); }
        return windows;
    }
    private static int appendNode(AccessibilityNodeInfo node, JSONArray nodes, int depth) throws Exception {
        if (depth > 80 || nodes.length() >= 5000)
            throw new IllegalStateException("Oracle forest traversal limit");
        int id = nodes.length();
        Rect bounds = new Rect(); node.getBoundsInScreen(bounds);
        JSONObject row = new JSONObject();
        row.put("id", id);
        row.put("text", string(node.getText()));
        row.put("content_description", string(node.getContentDescription()));
        row.put("class_name", string(node.getClassName()));
        row.put("hint_text", string(node.getHintText()));
        row.put("package_name", string(node.getPackageName()));
        row.put("view_id_resource_name", string(node.getViewIdResourceName()));
        row.put("is_checked", node.isChecked()); row.put("is_checkable", node.isCheckable());
        row.put("is_clickable", node.isClickable()); row.put("is_editable", node.isEditable());
        row.put("is_enabled", node.isEnabled()); row.put("is_focused", node.isFocused());
        row.put("is_focusable", node.isFocusable()); row.put("is_long_clickable", node.isLongClickable());
        row.put("is_scrollable", node.isScrollable()); row.put("is_selected", node.isSelected());
        row.put("is_visible_to_user", node.isVisibleToUser());
        row.put("bounds_in_screen", new JSONObject().put("left", bounds.left).put("right", bounds.right)
            .put("top", bounds.top).put("bottom", bounds.bottom));
        JSONArray children = new JSONArray(); row.put("child_ids", children); nodes.put(row);
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) throw new IllegalStateException("Missing oracle child");
            try { children.put(appendNode(child, nodes, depth + 1)); }
            finally { child.recycle(); }
        }
        return id;
    }
    private static String string(CharSequence value) { return value == null ? "" : value.toString(); }
    private static void clearCache(UiAutomation automation) throws Exception {
        Class<?> client = Class.forName("android.view.accessibility.AccessibilityInteractionClient");
        int id = (Integer) UiAutomation.class.getMethod("getConnectionId").invoke(automation);
        client.getMethod("clearCache", int.class)
            .invoke(client.getMethod("getInstance").invoke(null), id);
    }
    private static String identity(AccessibilityNodeInfo node) {
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        return node.getWindowId() + ":" + node.getPackageName() + ":"
            + node.getClassName() + ":" + bounds;
    }

    public static void main(String[] args) throws Exception {
        if ((args.length != 2 && args.length != 3) || android.os.Build.VERSION.SDK_INT != 33)
            throw new IllegalArgumentException("Requires output path, nonce and API 33");
        HandlerThread thread = new HandlerThread("oracle");
        thread.start();
        UiAutomation automation = null;
        try {
            Class<?> connection = Class.forName("android.app.IUiAutomationConnection");
            Object binder = Class.forName("android.app.UiAutomationConnection")
                .getConstructor().newInstance();
            automation = (UiAutomation) UiAutomation.class
                .getConstructor(Looper.class, connection).newInstance(thread.getLooper(), binder);
            // Keep other accessibility services running; never consume Collector state.
            UiAutomation.class.getMethod("connect", int.class).invoke(automation, 1);
            AccessibilityServiceInfo info = automation.getServiceInfo();
            info.flags |= AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
            info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
            info.flags |= AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
            automation.setServiceInfo(info);
            clearCache(automation);
            AccessibilityNodeInfo root = automation.getRootInActiveWindow();
            if (root == null) throw new IllegalStateException("No active root");
            String before = identity(root);
            String windowsBefore = windowsIdentity(automation);
            Class<?> manager = Class.forName("android.hardware.display.DisplayManagerGlobal");
            Display display = (Display) manager.getMethod("getRealDisplay", int.class)
                .invoke(manager.getMethod("getInstance").invoke(null), 0);
            Point size = new Point();
            display.getRealSize(size);
            int rotation = display.getRotation();
            File output = new File(args[0]);
            if (args.length == 3 && "forest".equals(args[2])) {
                JSONObject forest = new JSONObject().put("format", "oracle-forest-v1")
                    .put("windows", collectWindows(automation));
                try (FileOutputStream stream = new FileOutputStream(output)) {
                    stream.write(forest.toString().getBytes(StandardCharsets.UTF_8));
                }
            } else if (args.length == 2) {
                Class.forName("com.android.uiautomator.core.AccessibilityNodeInfoDumper")
                .getMethod("dumpWindowToFile", AccessibilityNodeInfo.class, File.class,
                           int.class, int.class, int.class)
                .invoke(null, root, output, rotation, size.x, size.y);
            } else throw new IllegalArgumentException("Unknown oracle format");
            clearCache(automation);
            AccessibilityNodeInfo after = automation.getRootInActiveWindow();
            if (after == null || !before.equals(identity(after)) || rotation != display.getRotation())
                throw new IllegalStateException("Active window changed during oracle read");
            if (!windowsBefore.equals(windowsIdentity(automation)))
                throw new IllegalStateException("Window structure changed during oracle read");
            if (!output.isFile() || output.length() == 0)
                throw new IllegalStateException("No XML produced");
            System.out.println("ORACLE_DUMP_OK " + args[1]);
        } catch (Throwable failure) {
            failure.printStackTrace(System.err);
            throw failure;
        } finally {
            try {
                if (automation != null) UiAutomation.class.getMethod("disconnect").invoke(automation);
            } finally {
                thread.quitSafely();
            }
        }
    }
}
