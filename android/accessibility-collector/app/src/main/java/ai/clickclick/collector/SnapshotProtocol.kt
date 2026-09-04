package ai.clickclick.collector

import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.EOFException

object SnapshotProtocol {
    const val VERSION = 1
    const val MAX_FRAME_BYTES = 8 * 1024 * 1024

    fun readFrame(input: DataInputStream): ByteArray? {
        val size = try {
            input.readInt()
        } catch (_: EOFException) {
            return null
        }
        require(size in 1..MAX_FRAME_BYTES) { "invalid frame size: $size" }
        return ByteArray(size).also(input::readFully)
    }

    fun writeFrame(output: DataOutputStream, payload: ByteArray) {
        require(payload.size in 1..MAX_FRAME_BYTES) {
            "invalid frame size: ${payload.size}"
        }
        output.writeInt(payload.size)
        output.write(payload)
        output.flush()
    }
}
