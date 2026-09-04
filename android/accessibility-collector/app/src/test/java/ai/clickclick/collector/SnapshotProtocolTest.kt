package ai.clickclick.collector

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream

class SnapshotProtocolTest {
    @Test fun frameRoundTrip() {
        val payload = "{\"operation\":\"snapshot\"}".toByteArray()
        val bytes = ByteArrayOutputStream().also {
            SnapshotProtocol.writeFrame(DataOutputStream(it), payload)
        }.toByteArray()
        assertArrayEquals(
            payload,
            SnapshotProtocol.readFrame(DataInputStream(ByteArrayInputStream(bytes))),
        )
    }

    @Test fun cleanEofReturnsNull() {
        assertNull(
            SnapshotProtocol.readFrame(
                DataInputStream(ByteArrayInputStream(byteArrayOf()))
            )
        )
    }

    @Test(expected = IllegalArgumentException::class)
    fun oversizedFrameIsRejected() {
        val header = ByteArrayOutputStream().also {
            DataOutputStream(it).writeInt(SnapshotProtocol.MAX_FRAME_BYTES + 1)
        }.toByteArray()
        SnapshotProtocol.readFrame(DataInputStream(ByteArrayInputStream(header)))
    }
}
