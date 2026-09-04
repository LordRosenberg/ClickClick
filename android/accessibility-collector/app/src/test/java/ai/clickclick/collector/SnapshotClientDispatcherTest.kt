package ai.clickclick.collector

import java.io.Closeable
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SnapshotClientDispatcherTest {
    private class RecordingCloseable : Closeable {
        val closeCount = AtomicInteger()
        override fun close() {
            closeCount.incrementAndGet()
        }
    }

    @Test
    fun deviceIdleTimeoutKeepsMarginAboveHostLease() {
        assertTrue(SnapshotSocketServer.CLIENT_IDLE_TIMEOUT_MS >= 90_000)
    }

    @Test
    fun idleClientDoesNotBlockSecondClient() {
        val dispatcher = SnapshotClientDispatcher(maxClients = 2)
        val releaseFirst = CountDownLatch(1)
        val firstStarted = CountDownLatch(1)
        val firstFinished = CountDownLatch(1)
        val secondFinished = CountDownLatch(1)
        try {
            assertTrue(dispatcher.dispatch {
                firstStarted.countDown()
                releaseFirst.await(2, TimeUnit.SECONDS)
                firstFinished.countDown()
            })
            assertTrue(firstStarted.await(1, TimeUnit.SECONDS))

            assertTrue(dispatcher.dispatch { secondFinished.countDown() })
            assertTrue(secondFinished.await(1, TimeUnit.SECONDS))
        } finally {
            releaseFirst.countDown()
            assertTrue(firstFinished.await(1, TimeUnit.SECONDS))
            dispatcher.stop()
        }
    }

    @Test
    fun rejectsOnlyClientBeyondBound() {
        val dispatcher = SnapshotClientDispatcher(maxClients = 1)
        val release = CountDownLatch(1)
        val started = CountDownLatch(1)
        val finished = CountDownLatch(1)
        try {
            assertTrue(dispatcher.dispatch {
                started.countDown()
                release.await(2, TimeUnit.SECONDS)
                finished.countDown()
            })
            assertTrue(started.await(1, TimeUnit.SECONDS))
            assertFalse(dispatcher.dispatch { error("must not run") })
        } finally {
            release.countDown()
            assertTrue(finished.await(1, TimeUnit.SECONDS))
            dispatcher.stop()
        }
    }

    @Test
    fun dispatcherRejectsAfterStop() {
        val dispatcher = SnapshotClientDispatcher(maxClients = 1)
        dispatcher.stop()
        dispatcher.stop()
        assertFalse(dispatcher.dispatch { error("must not run") })
    }

    @Test
    fun ownedClientsCloseExactlyOnceAndRejectAfterShutdown() {
        val clients = OwnedSnapshotClients<RecordingCloseable>()
        val first = RecordingCloseable()
        val second = RecordingCloseable()
        assertTrue(clients.addOrClose(first))
        assertTrue(clients.addOrClose(second))

        clients.closeAll()
        clients.closeAll()

        val rejected = RecordingCloseable()
        assertFalse(clients.addOrClose(rejected))

        assertEquals(1, first.closeCount.get())
        assertEquals(1, second.closeCount.get())
        assertEquals(1, rejected.closeCount.get())
    }

    @Test
    fun concurrentAddAndShutdownClosesEveryClientExactlyOnce() {
        val clients = OwnedSnapshotClients<RecordingCloseable>()
        val candidates = (1..256).map { RecordingCloseable() }
        val ready = CountDownLatch(1)
        val pool = Executors.newFixedThreadPool(8)
        val closeFinished = CountDownLatch(1)
        val closer = Thread {
            ready.await()
            clients.closeAll()
            closeFinished.countDown()
        }
        try {
            closer.start()
            candidates.forEach { client ->
                pool.execute {
                    ready.await()
                    clients.addOrClose(client)
                }
            }
            ready.countDown()
        } finally {
            pool.shutdown()
            assertTrue(pool.awaitTermination(2, TimeUnit.SECONDS))
            assertTrue(closeFinished.await(1, TimeUnit.SECONDS))
            closer.join(1_000)
            clients.closeAll()
        }

        assertTrue(candidates.all { it.closeCount.get() == 1 })
    }
}
