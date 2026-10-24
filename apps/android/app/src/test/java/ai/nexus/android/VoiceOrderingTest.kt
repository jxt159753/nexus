package ai.nexus.android

import ai.nexus.android.node.NodeConfig
import ai.nexus.android.client.uid
import ai.nexus.android.voice.CoreVoice
import ai.nexus.android.voice.VoiceMedia
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.ByteString.Companion.toByteString
import org.junit.Assert.*
import org.junit.Test
import java.nio.ByteBuffer
import java.net.InetAddress
import java.time.Instant
import java.util.concurrent.atomic.AtomicInteger

class VoiceOrderingTest {
    private class MemoryMedia : VoiceMedia {
        val played=AtomicInteger()
        @Volatile var closed=false
        override fun prepare() {}
        override suspend fun capture(send: (ByteArray)->Unit) { error("No microphone expected") }
        override fun stopCapture() {}
        override suspend fun play(pcm: ByteArray) { played.addAndGet(pcm.size) }
        override suspend fun drain() {}
        override fun flush() {}
        override fun close() { closed=true }
    }

    @Test fun audioWaitsForValidCommandAndExpiredCommandClosesWithoutPlayback() = runBlocking {
        checkOrdering(expired=true)
    }

    @Test fun audioArrivingBeforeCommandPlaysOnlyAfterValidCommand() = runBlocking {
        checkOrdering(expired=false)
    }

    private suspend fun checkOrdering(expired: Boolean) = coroutineScope {
        val session=uid();val conversation=uid();val utterance=uid()
        val fields=obj("voice_session_id" to str(session),"session_epoch" to JsonPrimitive(1))
        val clock=Instant.now()
        val controlReady=CompletableDeferred<WebSocket>()
        val audioReady=CompletableDeferred<WebSocket>()
        val stateReceived=CompletableDeferred<Unit>()
        fun message(type: String, extra: JsonObject)=voiceMessage(type,JsonObject(fields+extra),utc(clock))
        val server=MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(object: WebSocketListener() {
            override fun onClosing(webSocket: WebSocket,code: Int,reason: String) { webSocket.close(code,null) }
            override fun onMessage(webSocket: WebSocket,text: String) {
                when(wireJson.parseToJsonElement(text).jsonObject["type"]?.jsonPrimitive?.content) {
                    "voice.hello" -> {
                        webSocket.send(voiceMessage("voice.welcome",obj("heartbeat_interval_seconds" to JsonPrimitive(2)),utc(clock)))
                        webSocket.send(message("voice.session.started",obj("conversation_id" to str(conversation),"expires_at" to str(utc(clock.plusSeconds(30))))))
                    }
                    "voice.activate" -> {
                        webSocket.send(message("voice.state",obj("state" to str("speaking"),"generation" to JsonPrimitive(1),"utterance_id" to str(utterance))))
                        controlReady.complete(webSocket)
                    }
                }
            }
        }))
        server.enqueue(MockResponse().withWebSocketUpgrade(object: WebSocketListener() {
            override fun onOpen(webSocket: WebSocket,response: Response) { audioReady.complete(webSocket) }
            override fun onClosing(webSocket: WebSocket,code: Int,reason: String) { webSocket.close(code,null) }
        }))
        server.start(InetAddress.getByName("127.0.0.1"),0)
        val media=MemoryMedia()
        val runtime=CoreVoice(NodeConfig(server.url("/").newBuilder().host("127.0.0.1").build().toString(),uid(),"test-token-".repeat(5),emptySet()),uid(),session,conversation,media) { state,_ ->
            if(state=="speaking") stateReceived.complete(Unit)
        }
        val worker=launch(Dispatchers.Default) { runtime.run() }
        try {
            val control=withTimeout(5000){controlReady.await()}
            val audio=withTimeout(5000){audioReady.await()}
            withTimeout(5000){stateReceived.await()}
            val packet=ByteBuffer.allocate(13).putInt(0x4e585632).putInt(1).put(0).putInt(0).array()
            assertTrue(audio.send(packet.toByteString()))
            delay(150)
            assertEquals(0,media.played.get())
            control.send(message("voice.speak",obj("generation" to JsonPrimitive(1),"utterance_id" to str(utterance),"segment_index" to JsonPrimitive(0),"text" to str("test"),"not_after" to str(utc(clock.plusSeconds(if(expired) -10 else 10))))))
            if(expired) {
                withTimeout(5000){worker.join()}
                assertEquals(0,media.played.get())
                assertTrue(media.closed)
            } else {
                withTimeout(5000){while(media.played.get()==0) delay(10)}
                assertEquals(4,media.played.get())
            }
        } finally {
            runtime.stop();worker.cancelAndJoin()
            if(controlReady.isCompleted) controlReady.await().close(1000,null)
            if(audioReady.isCompleted) audioReady.await().close(1000,null)
            server.shutdown()
        }
    }
}
