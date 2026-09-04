package ai.clickclick.collector

import android.net.LocalServerSocket
import android.net.LocalSocket
import android.os.SystemClock
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
        try {
            val input = DataInputStream(client.inputStream)
            val output = DataOutputStream(client.outputStream)
            while (running) {
                val raw = SnapshotProtocol.readFrame(input) ?: break
                val request = JSONObject(raw.toString(Charsets.UTF_8))
                val requestId = request.optLong("request_id", -1L)
                val operation = request.optString("operation")
                val rejection = SnapshotRequestPolicy.rejection(
                    request.optInt("version", -1),
                    request.optString("token"),
                    token,
                    operation,
                )
                val response = when {
                    rejection != null -> errorResponse(requestId, rejection)
                    operation == "health" -> health().put("status", "ok")
                    else -> snapshot().put("status", "ok")
                }
                response
                    .put("version", SnapshotProtocol.VERSION)
                    .put("request_id", requestId)
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
        } catch (_: Exception) {
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
