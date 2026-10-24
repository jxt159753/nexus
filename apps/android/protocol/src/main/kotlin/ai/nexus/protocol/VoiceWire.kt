package ai.nexus.protocol

import kotlinx.serialization.json.*
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.time.Instant
import java.util.UUID

data class VoicePcmPacket(val generation: Long, val pcm: ByteArray, val end: Boolean)
fun unpackVoiceAudio(bytes: ByteArray): VoicePcmPacket {
    require(bytes.size in 9..8201 && (bytes.size-9)%2==0)
    val buffer=ByteBuffer.wrap(bytes).order(ByteOrder.BIG_ENDIAN)
    require(buffer.int==0x4e585632)
    val generation=buffer.int.toLong() and 0xffffffffL
    val flag=buffer.get().toInt()
    require(flag in 0..1 && (flag==0 || bytes.size==9))
    return VoicePcmPacket(generation,bytes.copyOfRange(9,bytes.size),flag==1)
}
fun voiceMessage(type: String, fields: JsonObject, timestamp: String = utc()): String = JsonObject(fields + mapOf(
    "type" to str(type),"protocol_version" to JsonPrimitive(1),"message_id" to str(UUID.randomUUID().toString()),"correlation_id" to JsonNull,"sent_at" to str(timestamp))).toString().also { require(it.toByteArray().size<=16384) }

/** Control messages are separate from the Node execution envelope. */
fun parseVoice(text: String): JsonObject {
    require(text.toByteArray().size<=16384)
    val value=wireJson.parseToJsonElement(text).jsonObject
    require(value.getValue("protocol_version").jsonPrimitive.int==1)
    UUID.fromString(value.getValue("message_id").jsonPrimitive.content)
    Instant.parse(value.getValue("sent_at").jsonPrimitive.content)
    val base=setOf("type","protocol_version","message_id","correlation_id","sent_at")
    val session=setOf("voice_session_id","session_epoch")
    val fields=when(value.getValue("type").jsonPrimitive.content) {
        "voice.welcome" -> setOf("connection_id","heartbeat_interval_seconds","heartbeat_timeout_seconds","policy_revision","max_session_seconds","max_final_chars")
        "voice.session.started" -> session+setOf("conversation_id","mode","expires_at")
        "voice.listen.start" -> session+setOf("utterance_id","max_seconds","language")
        "voice.listen.stop" -> session+setOf("reason")
        "voice.partial" -> session+setOf("utterance_id","seq","text","stable")
        "voice.final" -> session+setOf("utterance_id","text","started_at","ended_at","endpoint_reason")
        "voice.state" -> session+setOf("state","since","detail","generation","utterance_id")
        "voice.speak" -> session+setOf("utterance_id","generation","segment_index","text","final_segment","not_after","voice","speed")
        "voice.speak.stop" -> session+setOf("utterance_id","generation","reason","at_segment")
        "voice.speak.completed" -> session+setOf("utterance_id","generation","segments","total_ms")
        "voice.stop" -> session+setOf("reason")
        "voice.session.stopped" -> session+setOf("reason","capture_stopped_at","playback_stopped_at","dropped_frames","tts_cancelled","asr_cancelled","execution_stopped")
        "voice.error" -> session+setOf("code","message","retryable")
        else -> error("Unsupported voice message")
    }
    require(value.keys.all { it in base+fields })
    value["voice_session_id"]?.jsonPrimitive?.contentOrNull?.let { UUID.fromString(it);require(value.getValue("session_epoch").jsonPrimitive.int>=1) }
    value["generation"]?.jsonPrimitive?.long?.let { require(it in 0..0xffffffffL) }
    return value
}
