package ai.clickclick.collector

import android.content.Context
import android.os.PowerManager
import android.os.SystemClock
import org.json.JSONObject
import java.io.Closeable
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

internal object ScreenBrightLeasePolicy {
    const val MIN_TTL_MS = 10_000L
    const val MAX_TTL_MS = 120_000L
    const val MAX_LEASE_ID_LENGTH = 128

    fun rejection(leaseId: String, ttlMs: Long): String? = when {
        leaseId.isBlank() -> "missing_lease_id"
        leaseId.length > MAX_LEASE_ID_LENGTH -> "lease_id_too_long"
        ttlMs !in MIN_TTL_MS..MAX_TTL_MS -> "ttl_out_of_range"
        else -> null
    }
}

/**
 * Renewable task-scoped screen-bright lease.
 *
 * Normal task cleanup releases it immediately. A short TTL releases it after
 * host/ADB loss, and every platform WakeLock has its own slightly longer
 * timeout in case the local scheduler stalls. Android also releases the lock
 * when this service process dies.
 */
internal class ScreenBrightLeaseController(context: Context) : Closeable {
    private val powerManager = context.getSystemService(PowerManager::class.java)
    private val scheduler = Executors.newSingleThreadScheduledExecutor { runnable ->
        Thread(runnable, "clickclick-screen-lease").apply { isDaemon = true }
    }
    private val lockSequence = AtomicLong(0)
    private var activeLeaseId: String? = null
    private var expiresAtMs = 0L
    private var wakeLock: PowerManager.WakeLock? = null
    private var expiryFuture: ScheduledFuture<*>? = null
    private var renewalCount = 0L
    private var lastReleaseReason = "never_acquired"
    private var closed = false

    fun handle(request: JSONObject): JSONObject = synchronized(this) {
        when (request.optString("operation")) {
            "power_lease_acquire" -> acquireLocked(request, requireExisting = false)
            "power_lease_renew" -> acquireLocked(request, requireExisting = true)
            "power_lease_release" -> releaseRequestLocked(request.optString("lease_id"))
            "power_lease_status" -> statusLocked()
            else -> JSONObject().put("lease_state", "error")
                .put("error_detail", "unsupported_operation")
        }
    }

    fun status(): JSONObject = synchronized(this) { statusLocked() }

    private fun acquireLocked(request: JSONObject, requireExisting: Boolean): JSONObject {
        if (closed) {
            return statusLocked().put("lease_state", "error")
                .put("error_detail", "controller_closed")
        }
        val leaseId = request.optString("lease_id")
        val ttlMs = request.optLong("ttl_ms", -1L)
        ScreenBrightLeasePolicy.rejection(leaseId, ttlMs)?.let { reason ->
            return statusLocked().put("lease_state", "error").put("error_detail", reason)
        }
        if (requireExisting && activeLeaseId != leaseId) {
            return statusLocked().put("lease_state", "stale")
                .put("error_detail", "lease_mismatch")
        }

        // Acquire the replacement first. Releasing the prior generation only
        // afterwards prevents a brief dimming gap during renewal.
        val replacement = try {
            newWakeLock(ttlMs)
        } catch (error: Exception) {
            return statusLocked().put("lease_state", "error").put(
                "error_detail",
                "wake_lock_acquire_failed:${error.javaClass.simpleName}",
            )
        }
        val previous = wakeLock
        wakeLock = replacement
        activeLeaseId = leaseId
        expiresAtMs = SystemClock.elapsedRealtime() + ttlMs
        renewalCount = if (requireExisting) renewalCount + 1 else 0
        lastReleaseReason = ""
        expiryFuture?.cancel(false)
        val expectedLeaseId = leaseId
        val expectedExpiry = expiresAtMs
        expiryFuture = scheduler.schedule(
            { expire(expectedLeaseId, expectedExpiry) }, ttlMs, TimeUnit.MILLISECONDS,
        )
        releaseWakeLock(previous)
        return statusLocked().put("lease_state", "active")
    }

    @Suppress("DEPRECATION")
    private fun newWakeLock(ttlMs: Long): PowerManager.WakeLock {
        val flags = PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP
        return powerManager.newWakeLock(
            flags,
            "ClickClick:TaskScreen:${lockSequence.incrementAndGet()}",
        ).also {
            it.setReferenceCounted(false)
            it.acquire(ttlMs + PLATFORM_TIMEOUT_MARGIN_MS)
        }
    }

    private fun expire(expectedLeaseId: String, expectedExpiry: Long) = synchronized(this) {
        if (activeLeaseId == expectedLeaseId && expiresAtMs == expectedExpiry) {
            releaseLocked("ttl_expired")
        }
    }

    private fun releaseRequestLocked(leaseId: String): JSONObject {
        if (activeLeaseId == null) {
            return statusLocked().put("lease_state", "released").put("released", false)
        }
        if (leaseId != activeLeaseId) {
            return statusLocked().put("lease_state", "stale").put("released", false)
                .put("error_detail", "lease_mismatch")
        }
        releaseLocked("host_release")
        return statusLocked().put("lease_state", "released").put("released", true)
    }

    private fun releaseLocked(reason: String) {
        expiryFuture?.cancel(false)
        expiryFuture = null
        val owned = wakeLock
        wakeLock = null
        activeLeaseId = null
        expiresAtMs = 0L
        lastReleaseReason = reason
        releaseWakeLock(owned)
    }

    private fun releaseWakeLock(owned: PowerManager.WakeLock?) {
        if (owned == null) return
        try {
            if (owned.isHeld) owned.release()
        } catch (_: RuntimeException) {
            // Platform timeout or a physical power-button event may win the race.
        }
    }

    private fun statusLocked(): JSONObject {
        val remaining = (expiresAtMs - SystemClock.elapsedRealtime()).coerceAtLeast(0L)
        return JSONObject()
            .put("active", activeLeaseId != null && remaining > 0)
            .put("held", wakeLock?.isHeld == true)
            .put("lease_id", activeLeaseId.orEmpty())
            .put("expires_in_ms", remaining)
            .put("max_ttl_ms", ScreenBrightLeasePolicy.MAX_TTL_MS)
            .put("renewal_count", renewalCount)
            .put("last_release_reason", lastReleaseReason)
    }

    override fun close() {
        synchronized(this) {
            if (closed) return
            closed = true
            releaseLocked("service_destroyed")
        }
        scheduler.shutdownNow()
    }

    companion object {
        private const val PLATFORM_TIMEOUT_MARGIN_MS = 5_000L
    }
}
