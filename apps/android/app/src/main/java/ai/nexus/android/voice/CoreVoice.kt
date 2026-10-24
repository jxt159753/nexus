package ai.nexus.android.voice

import android.os.SystemClock
import ai.nexus.android.client.*
import ai.nexus.android.node.NodeConfig
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import kotlinx.serialization.json.*
import okhttp3.*
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.time.Instant
import java.util.concurrent.TimeUnit

/** One explicitly approved session. No reconnect, audio replay or automatic paid retry. */
class CoreVoice(private val config: NodeConfig, private val requestId: String, private val sessionId: String,
    private val conversationId: String, private val media: VoiceMedia, private val report: (String,String) -> Unit) {
    private val client=OkHttpClient.Builder().retryOnConnectionFailure(false).followRedirects(false).followSslRedirects(false).connectTimeout(8,TimeUnit.SECONDS).pingInterval(10,TimeUnit.SECONDS).build()
    private var control: WebSocket?=null
    private var audio: WebSocket?=null
    @Volatile private var stopped=false
    @Volatile private var state="activating"
    @Volatile private var generation=0L
    @Volatile private var playable=-1L
    @Volatile private var blocked=-1L
    @Volatile private var utterance=""
    private var segments=0
    private var epoch=0
    private var serverTime=Instant.now()
    private var serverClock=SystemClock.elapsedRealtime()
    private var job: Job?=null
    private var capture: Job?=null
    private fun now()=serverTime.plusMillis(SystemClock.elapsedRealtime()-serverClock)
    private fun session()=obj("voice_session_id" to str(sessionId),"session_epoch" to JsonPrimitive(epoch))
    private fun send(type: String, fields: JsonObject) {
        check(control?.queueSize()?.let{it<65536}==true && control?.send(voiceMessage(type,fields,utc(now())))==true)
    }
    private fun change(value: String, detail: String="") { state=value;report(value,detail) }
    fun stop() {
        if(stopped) return
        stopped=true;capture?.cancel();media.close()
        if(epoch>0) runCatching { send("voice.stop.request",JsonObject(session()+mapOf("reason" to str("user"),"local_stopped_at" to str(utc(now()))))) }
        audio?.close(1000,null);control?.close(1000,null);job?.cancel()
        report("stopped","麦克风和播放已关闭")
    }
    fun interrupt() {
        if(stopped || state !in setOf("thinking","speaking") || utterance.isEmpty()) return
        blocked=maxOf(blocked,generation);media.flush()
        runCatching { send("voice.barge_in",JsonObject(session()+mapOf("utterance_id" to str(utterance),"generation" to JsonPrimitive(generation),
            "detected_at" to str(utc(now())),"playback_position_ms" to JsonPrimitive(0),"segment_index" to JsonPrimitive(maxOf(0,segments-1)),"confidence" to JsonNull,"source" to str("button")))) }
            .onFailure { stop() }
        change("interrupted","已停止当前播放，等待 Core 开始收听")
    }
    private data class Event(val text: String?=null,val bytes: ByteArray?=null,val opened: Boolean=false)
    private fun connect(path: String, events: Channel<Event>, binary: Boolean): WebSocket {
        val url=coreUrl(config.url).resolve(path)!!
        return client.newWebSocket(Request.Builder().url(url).header("Authorization","Bearer ${config.token}").build(),object: WebSocketListener() {
            override fun onOpen(webSocket: WebSocket,response: Response) { if(!events.trySend(Event(opened=true)).isSuccess) webSocket.cancel() }
            override fun onMessage(webSocket: WebSocket,text: String) { if(binary || text.toByteArray().size>16384 || !events.trySend(Event(text=text)).isSuccess) { webSocket.cancel();events.close() } }
            override fun onMessage(webSocket: WebSocket,bytes: ByteString) {
                if(!binary || bytes.size !in 9..8201) { webSocket.cancel();events.close();return }
                try { runBlocking { withTimeout(3000) { events.send(Event(bytes=bytes.toByteArray())) } } }
                catch(error: Exception) { webSocket.cancel();events.close() }
            }
            override fun onClosing(webSocket: WebSocket,code: Int,reason: String) { events.close();webSocket.close(code,null) }
            override fun onFailure(webSocket: WebSocket,t: Throwable,response: Response?) { events.close() }
        })
    }
    suspend fun run() = coroutineScope {
        job=currentCoroutineContext()[Job]
        val events=Channel<Event>(32)
        val packets=Channel<Event>(16)
        try {
            check(!stopped)
            media.prepare()
            control=connect("/ws/v1/voice",events,false)
            require(withTimeout(8000){events.receive()}.opened)
            send("voice.hello",obj("node_id" to str(config.nodeId),"voice_version" to str("android-0.1.0"),"boot_id" to str(uid()),
                "features" to JsonArray(listOf("capture","playback","streaming_asr","streaming_tts").map(::str)),"models" to obj(),"config_digest" to JsonNull,"audio_framing" to str("nxv2"),"activation_request_id" to str(requestId)))
            val welcome=parseVoice(requireNotNull(withTimeout(8000){events.receive()}.text))
            require(welcome.text("type")=="voice.welcome")
            serverTime=Instant.parse(welcome.text("sent_at"));serverClock=SystemClock.elapsedRealtime()
            val heartbeat=welcome.text("heartbeat_interval_seconds").toLong().also { require(it in 1..10) }
            val first=parseVoice(requireNotNull(withTimeout(12000){events.receive()}.text))
            require(first.text("type")=="voice.session.started" && first.text("voice_session_id")==sessionId && first.text("conversation_id")==conversationId)
            epoch=first.text("session_epoch").toInt()
            val remaining=java.time.Duration.between(now(),Instant.parse(first.text("expires_at"))).toMillis().coerceAtMost(3600000)
            require(remaining>0)
            audio=connect("/ws/v1/voice/audio?session_id=$sessionId&epoch=$epoch",packets,true)
            require(withTimeout(8000){packets.receive()}.opened)
            send("voice.activate",obj("request_id" to str(requestId),"trigger" to str("button"),"confidence" to JsonNull,"device_id" to JsonNull,"conversation_id" to str(conversationId)))
            launch { delay(remaining);stop() }
            launch {
                while(isActive && !stopped) {
                    send("voice.heartbeat",JsonObject(session()+mapOf("state" to str(state),"active_utterance_id" to JsonNull,"capture_active" to JsonPrimitive(capture?.isActive==true),"playback_active" to JsonPrimitive(state=="speaking"))))
                    delay(heartbeat*1000)
                }
            }
            launch {
                for(event in packets) {
                    val packet=unpackVoiceAudio(requireNotNull(event.bytes))
                    if(packet.generation<=blocked || packet.generation<generation) continue
                    // Separate sockets can deliver PCM before its valid control command.
                    withTimeout(2000) { while(packet.generation>playable && packet.generation>blocked && !stopped) delay(10) }
                    if(packet.generation!=generation || packet.generation!=playable || packet.generation<=blocked || stopped) continue
                    if(packet.end) {
                        media.drain()
                        if(packet.generation==generation && packet.generation>blocked && !stopped) send("voice.speak.completed",JsonObject(session()+mapOf("utterance_id" to str(utterance),"generation" to JsonPrimitive(generation),"segments" to JsonPrimitive(segments),"total_ms" to JsonPrimitive(0))))
                    } else media.play(packet.pcm)
                }
                error("Audio disconnected")
            }
            for(event in events) {
                val value=parseVoice(requireNotNull(event.text))
                require(value.text("voice_session_id").isEmpty() || value.text("voice_session_id")==sessionId && value.text("session_epoch").toInt()==epoch)
                when(value.text("type")) {
                    "voice.session.started" -> require(value.text("conversation_id")==conversationId)
                    "voice.listen.start" -> {
                        capture?.cancelAndJoin();media.stopCapture();change("listening","正在收听 · 音频经 Core 外发")
                        val seconds=value.text("max_seconds").toLong().also{require(it in 1..60)}
                        capture=launch {
                            try { withTimeout(seconds*1000) { media.capture { pcm ->
                                if(!stopped && state=="listening") {
                                    check(pcm.size in 2..3200 && pcm.size%2==0)
                                    check(audio?.queueSize()?.let{it<=32000}==true && audio?.send(pcm.toByteString())==true)
                                }
                            } } } catch(timeout: TimeoutCancellationException) { stop() }
                        }
                    }
                    "voice.listen.stop" -> { capture?.cancelAndJoin();media.stopCapture() }
                    "voice.partial","voice.final" -> { require(value.text("text").length<=2000);report(state,value.text("text")) }
                    "voice.state" -> {
                        generation=maxOf(generation,value.text("generation").toLong())
                        value.text("utterance_id").takeIf{it.isNotEmpty()}?.let { utterance=it }
                        val next=value.text("state");require(next in setOf("activating","listening","thinking","speaking","interrupted","stopping","stopped","error"))
                        if(next!="listening") { capture?.cancelAndJoin();media.stopCapture() }
                        change(next,value.text("detail"))
                    }
                    "voice.speak" -> {
                        val next=value.text("generation").toLong()
                        if(next>blocked && next>=generation) {
                            require(Instant.parse(value.text("not_after")).isAfter(now())) { "Playback command expired" }
                            capture?.cancelAndJoin();media.stopCapture()
                            generation=next;utterance=value.text("utterance_id");segments=value.text("segment_index").toInt()+1
                            playable=next
                            change("speaking",value.text("text"))
                        }
                    }
                    "voice.speak.stop" -> { blocked=maxOf(blocked,value.text("generation").toLong());media.flush();change("interrupted") }
                    "voice.speak.completed" -> if(value.text("generation").toLong()==generation) segments=value.text("segments").toInt()
                    "voice.stop" -> {
                        capture?.cancelAndJoin();media.close()
                        send("voice.session.stopped",JsonObject(session()+mapOf("reason" to str(value.text("reason")),"capture_stopped_at" to str(utc(now())),"playback_stopped_at" to str(utc(now())),"execution_stopped" to JsonPrimitive(true))))
                        stopped=true;change("stopped","麦克风和播放已关闭");break
                    }
                    "voice.session.stopped" -> { stopped=true;media.close();change("stopped");break }
                    "voice.error" -> error("Voice operation rejected")
                    else -> error("Unexpected voice message")
                }
            }
            if(!stopped) error("Control disconnected")
        } catch(cancel: CancellationException) { throw cancel }
        catch(error: Exception) { report("error","语音连接或音频设备不可用，已关麦。请检查配置后手动重试。") }
        finally {
            stopped=true;media.close();coroutineContext.cancelChildren();events.close();packets.close()
            audio?.cancel();control?.cancel();client.dispatcher.executorService.shutdown();client.connectionPool.evictAll()
        }
    }
}
