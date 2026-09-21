package ai.clickclick.collector

import android.net.LocalServerSocket
import android.net.LocalSocket
import android.os.SystemClock
import android.util.Log
import org.json.JSONObject
import java.io.Closeable
import java.io.DataInputStream
import java.io.DataOutputStream
import java.security.SecureRandom
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.SynchronousQueue
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit

internal class SnapshotClientDispatcher(
    maxClients: Int = SnapshotSocketServer.MAX_CLIENTS,
) {
    private val executor = ThreadPoolExecutor(
        0,
        maxClients,
        30L,
        TimeUnit.SECONDS,
        SynchronousQueue(),
        { runnable ->
            Thread(runnable, "clickclick-a11y-client").apply { isDaemon = true }
        },
    )

    fun dispatch(block: () -> Unit): Boolean = try {
        executor.execute(block)
        true
    } catch (_: RejectedExecutionException) {
        false
    }

    fun stop() {
        executor.shutdownNow()
    }
}

internal class OwnedSnapshotClients<T : Closeable> {
    private val clients = mutableSetOf<T>()
    private var accepting = true

    fun addOrClose(client: T): Boolean {
        val accepted = synchronized(this) {
            if (accepting) {
                clients.add(client)
                true
            } else {
                false
            }
        }
        if (!accepted) closeQuietly(client)
        return accepted
    }

    fun remove(client: T): Boolean = synchronized(this) { clients.remove(client) }

    fun closeAll() {
        val owned = synchronized(this) {
            accepting = false
            clients.toList().also { clients.clear() }
        }
        owned.forEach(::closeQuietly)
    }

    private fun closeQuietly(client: T) {
        try { client.close() } catch (_: Exception) { }
    }
}

class SnapshotSocketServer(
    private val snapshot: () -> JSONObject,
    private val health: () -> JSONObject,
    private val powerLease: (JSONObject) -> JSONObject,
    private val eventCursor: () -> Long,
    private val eventsAfter: (Long, Int) -> JSONObject,
    private val nodeClick: (JSONObject) -> JSONObject,
) {
    val socketName = "clickclick_a11y_${randomHex(8)}"
    val token = randomHex(16)
    private val acceptExecutor = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "clickclick-a11y-accept").apply { isDaemon = true }
    }
    private val clientDispatcher = SnapshotClientDispatcher()
    private val clients = OwnedSnapshotClients<LocalSocket>()
    private val lifecycleLock = Any()
    @Volatile private var running = false
    @Volatile private var server: LocalServerSocket? = null

    fun start() {
        val listener = synchronized(lifecycleLock) {
            if (running) return
            LocalServerSocket(socketName).also {
                server = it
                running = true
            }
        }
        try {
            acceptExecutor.execute {
                try {
                    while (running) {
                        val client = try {
                            listener.accept()
                        } catch (_: Exception) {
                            break
                        }
                        if (!clients.addOrClose(client)) break
                        if (!clientDispatcher.dispatch { serve(client) }) {
                            if (clients.remove(client)) closeQuietly(client)
                        }
                    }
                } finally {
                    shutdownTransport(listener)
                }
            }
        } catch (error: RejectedExecutionException) {
            shutdownTransport(listener)
            throw error
        }
    }

    fun stop() {
        shutdownTransport()
    }

    private fun shutdownTransport(expectedListener: LocalServerSocket? = null) {
        val listener = synchronized(lifecycleLock) {
            running = false
            val current = server
            if (expectedListener == null || current === expectedListener) server = null
            current
        }
        closeQuietly(listener)
        clients.closeAll()
        clientDispatcher.stop()
        acceptExecutor.shutdownNow()
    }

    private fun serve(client: LocalSocket) {
        client.soTimeout = CLIENT_IDLE_TIMEOUT_MS
        var phase = "read_request"
        try {
            val input = DataInputStream(client.inputStream)
            val output = DataOutputStream(client.outputStream)
            while (running) {
                phase = "read_request"
                val raw = SnapshotProtocol.readFrame(input) ?: break
                val request = JSONObject(raw.toString(Charsets.UTF_8))
                val requestId = request.optLong("request_id", -1L)
                val operation = request.optString("operation")
                phase = if (operation == "snapshot") "capture_snapshot" else "handle_request"
                val rejection = SnapshotRequestPolicy.rejection(
                    request.optInt("version", -1),
                    request.optString("token"),
                    token,
                    operation,
                )
                val response = when {
                    rejection != null -> errorResponse(requestId, rejection)
                    operation == "health" -> health().put("status", "ok")
                    operation == "snapshot" -> snapshot().put("status", "ok")
                    operation == "node_click" -> nodeClick(request).put("status", "ok")
                    operation == "events_cursor" -> JSONObject()
                        .put("status", "ok")
                        .put("current_sequence", eventCursor())
                    operation == "events_after_sequence" -> {
                        val afterSequence = request.optLong("after_sequence", -1L)
                        val limit = request.optInt("limit", 0)
                        val eventRejection = InteractionEventRequestPolicy.rejection(
                            afterSequence, limit,
                        )
                        if (eventRejection != null) errorResponse(requestId, eventRejection)
                        else eventsAfter(afterSequence, limit).put("status", "ok")
                    }
                    else -> powerLease(request).put("status", "ok")
                }
                response
                    .put("version", SnapshotProtocol.VERSION)
                    .put("request_id", requestId)
                phase = "write_response"
                val serializationStarted = SystemClock.elapsedRealtimeNanos()
                response.put("serialization_elapsed_ms", 0.0)
                response.toString()
                response.put(
                    "serialization_elapsed_ms",
                    (SystemClock.elapsedRealtimeNanos() - serializationStarted) / 1_000_000.0,
                )
                SnapshotProtocol.writeFrame(
                    output, response.toString().toByteArray(Charsets.UTF_8)
                )
                if (response.optString("error") == "authentication_failed") break
            }
        } catch (error: Exception) {
            // Do not log authenticated request bodies. Preserve the stage and
            // stack so platform traversal failures are distinguishable from
            // transport EOF / a client disconnect in live investigations.
            Log.w("ClickClickCollector", "Channel closed during $phase", error)
            // Transport errors are represented by EOF to the host, which owns
            // one bounded reconnect before entering compatibility fallback.
        } finally {
            if (clients.remove(client)) closeQuietly(client)
        }
    }

    private fun closeQuietly(closeable: Closeable?) {
        try { closeable?.close() } catch (_: Exception) { }
    }

    private fun errorResponse(requestId: Long, reason: String): JSONObject =
        JSONObject()
            .put("status", "error")
            .put("request_id", requestId)
            .put("error", reason)

    companion object {
        internal const val MAX_CLIENTS = 8
        // Host channels close after 60 s of inactivity. Keep a wide margin so
        // event-loop scheduling cannot leave a half-closed writer marked ready.
        internal const val CLIENT_IDLE_TIMEOUT_MS = 90_000
        private val random = SecureRandom()

        private fun randomHex(bytes: Int): String = ByteArray(bytes)
            .also(random::nextBytes)
            .joinToString("") { "%02x".format(it) }
    }
}
