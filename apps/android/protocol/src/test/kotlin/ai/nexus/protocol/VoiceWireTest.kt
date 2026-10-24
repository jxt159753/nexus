package ai.nexus.protocol

import org.junit.Assert.*
import org.junit.Test
import java.nio.ByteBuffer

class VoiceWireTest {
    @Test fun networkHeaderAndPcmAreValidatedWithoutDecodingOldGenerations() {
        val frame=ByteBuffer.allocate(13).putInt(0x4e585632).putInt(-1).put(0).put(byteArrayOf(1,2,3,4)).array()
        val parsed=unpackVoiceAudio(frame)
        assertEquals(4294967295L,parsed.generation);assertArrayEquals(byteArrayOf(1,2,3,4),parsed.pcm);assertFalse(parsed.end)
        assertTrue(unpackVoiceAudio(ByteBuffer.allocate(9).putInt(0x4e585632).putInt(2).put(1).array()).end)
        assertThrows(Exception::class.java) { unpackVoiceAudio(frame.copyOf(10)) }
        frame[8]=1;assertThrows(Exception::class.java) { unpackVoiceAudio(frame) }
        frame[0]=0;assertThrows(Exception::class.java) { unpackVoiceAudio(frame) }
    }
    @Test fun rejectsUnexpectedVoiceControlAndOversize() {
        assertThrows(Exception::class.java) { parseVoice(voiceMessage("voice.unrecognized",obj())) }
        assertThrows(Exception::class.java) { parseVoice(" ".repeat(16385)) }
    }
}
