package ai.nexus.android

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import ai.nexus.android.client.*
import ai.nexus.android.node.NodeConfig
import ai.nexus.android.security.Vault
import ai.nexus.android.voice.*
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

@RunWith(AndroidJUnit4::class)
class VoiceIntegrationTest {
    private class SilentMedia : VoiceMedia {
        val captures=AtomicInteger();val played=AtomicInteger();val drains=AtomicInteger();val flushed=AtomicInteger()
        @Volatile var closed=false
        override fun prepare() {}
        override suspend fun capture(send: (ByteArray)->Unit) { captures.incrementAndGet();while(currentCoroutineContext().isActive && !closed){send(ByteArray(3200));delay(100)} }
        override fun stopCapture() {}
        override suspend fun play(pcm: ByteArray) { played.addAndGet(pcm.size);delay(10) }
        override suspend fun drain() { delay(100);drains.incrementAndGet() }
        override fun flush() { flushed.incrementAndGet() }
        override fun close() { closed=true }
    }
    @Test fun realCoreVoiceUsesFramedAudioAcknowledgesDrainAndStops() = runBlocking {
        val context=InstrumentationRegistry.getInstrumentation().targetContext
        val file=File(context.filesDir,"android-fixture.json")
        assumeTrue(file.exists())
        val fixture=wireJson.parseToJsonElement(file.readText()).jsonObject
        val client=CoreClient(Vault(context,"voice-test-owner"))
        client.login(fixture.text("url"),fixture.text("owner_token"))
        val conv=client.request("POST","/api/v1/chat/conversations",obj("request_id" to str(uid()))).objectAt("conversation").text("conversation_id")
        val request=uid()
        val prepared=client.request("POST","/api/v1/voice/sessions",obj("request_id" to str(request),"node_id" to str(fixture.text("node_id")),"conversation_id" to str(conv),"allow_audio_model" to JsonPrimitive(true),"allow_model" to JsonPrimitive(true)))
        val session=prepared.text("voice_session_id")
        val media=SilentMedia()
        val state=AtomicReference("")
        val runtime=CoreVoice(NodeConfig.from(fixture),request,session,conv,media) { value,_ -> state.set(value) }
        val worker=launch(Dispatchers.IO) { runtime.run() }
        try {
            withTimeout(20000) { while(media.drains.get()<1 || media.captures.get()<2) { check(worker.isActive) { "voice exited before a complete round trip: ${state.get()}" };delay(20) } }
            assertTrue(media.played.get()>0)
            val history=client.request("GET","/api/v1/chat/conversations/$conv/turns")
            assertTrue(history.objects("turns").any{it.text("text").contains("语音测试")})
            withTimeout(10000) { while(state.get() !in setOf("thinking","speaking")) delay(10) }
            runtime.interrupt()
            assertTrue(media.flushed.get()>0)
            runtime.stop();withTimeout(5000){worker.join()}
            assertTrue(media.closed)
            client.request("POST","/api/v1/voice/sessions/$session/stop",obj())
            assertFalse(client.request("GET","/api/v1/voice").objects("sessions").any{it.text("voice_session_id")==session})
        } finally { runtime.stop();worker.cancelAndJoin();client.logout() }
    }
}
