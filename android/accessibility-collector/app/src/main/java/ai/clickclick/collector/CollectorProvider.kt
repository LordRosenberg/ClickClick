package ai.clickclick.collector

import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.util.Base64
import org.json.JSONObject

class CollectorProvider : ContentProvider() {
    override fun onCreate(): Boolean = true

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?,
    ): Cursor {
        val service = CollectorService.instance
        val column = if (uri.lastPathSegment == "health") "health" else "snapshot"
        val payload = when (column) {
            "health" -> service?.health() ?: JSONObject()
                .put("ready", false)
                .put("service", "not_connected")
            else -> service?.snapshot() ?: JSONObject()
                .put("schema_version", 1)
                .put("generation", 0)
                .put("captured_monotonic_ms", 0.0)
                .put("complete", false)
                .put("reasons", org.json.JSONArray().put("service_not_connected"))
                .put("windows", org.json.JSONArray())
        }
        val encoded = Base64.encodeToString(
            payload.toString().toByteArray(Charsets.UTF_8), Base64.NO_WRAP
        )
        return MatrixCursor(arrayOf(column)).apply { addRow(arrayOf(encoded)) }
    }

    override fun getType(uri: Uri): String = "application/json"
    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int = 0
    override fun update(
        uri: Uri,
        values: ContentValues?,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Int = 0
}
