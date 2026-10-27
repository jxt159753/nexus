package ai.nexus.android.node

import android.content.Context
import android.os.SystemClock
import ai.nexus.android.client.coreUrl
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import kotlinx.serialization.json.*
import okhttp3.*
import java.time.Instant
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.random.Random

data class NodeConfig(val url: String, val nodeId: String, val token: String, val packages: Set<String>, val receiveFiles: Boolean = true) {
    init {
        coreUrl(url); UUID.fromString(nodeId)
        require(token.matches(Regex("[A-Za-z0-9_-]{43,128}")))
        require(packages.size <= 32 && packages.all { it.matches(Regex("[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z0-9_]+)+")) && it.length <= 200 })
    }
    companion object {
        fun from(value: JsonObject) = NodeConfig(value.getValue("url").jsonPrimitive.content, value.getValue("node_id").jsonPrimitive.content,
            value.getValue("token").jsonPrimitive.content, value.getValue("packages").jsonArray.map { it.jsonPrimitive.content }.toSet(), value["receive_files"]?.jsonPrimitive?.boolean ?: true)
    }
}

class NodeLink(private val context: Context, private val config: NodeConfig, private val report: (String) -> Unit) {
    private val client = OkHttpClient.Builder().retryOnConnectionFailure(false).followRedirects(false).followSslRedirects(false)
        .connectTimeout(5,TimeUnit.SECONDS).pingInterval(10,TimeUnit.SECONDS).build()
    private var socket: WebSocket? = null
    @Volatile private var stopping = false
    private val boot = UUID.randomUUID().toString()
    private var serverTime = Instant.EPOCH
    private var clockStart = 0L
    private var messageLimit = MAX_MESSAGE
    private fun now() = serverTime.plusMillis(SystemClock.elapsedRealtime() - clockStart)
    @Volatile private var activeTransfer: Triple<String,String,FileReceiver>? = null
    @Volatile private var activePhone: Triple<String,String,PhoneOperation>? = null
    fun stop() { stopping = true; activeTransfer?.third?.cancel(); activePhone?.third?.cancel(); socket?.cancel() }
    private fun send(type: String, payload: JsonObject, correlation: String? = null) {
        val encoded = message(type,payload,correlation)
        require(encoded.toByteArray().size <= messageLimit)
        check(!stopping && socket?.send(encoded) == true) { "连接已断开" }
    }
    private data class Event(val text: String? = null, val code: Int? = null)

    suspend fun run() {
        val processName=if(android.os.Build.VERSION.SDK_INT>=28) android.app.Application.getProcessName()
            else context.getSystemService(android.app.ActivityManager::class.java).runningAppProcesses?.firstOrNull{it.pid==android.os.Process.myPid()}?.processName
        check(processName=="${context.packageName}:node") { "设备执行必须位于独立 Node 进程" }
        val watchdog=Executors.newSingleThreadScheduledExecutor()
        val store = ReceiptStore(context, sha256("${coreUrl(config.url)}:${config.nodeId}"))
        val tools = DeviceTools(context)
        val access = PhoneAccess(context)
        NotificationAccess.configure(config.packages)
        var delaySeconds = 1L
        try {
            while (!stopping && currentCoroutineContext().isActive) {
                val events = Channel<Event>(32)
                val url = coreUrl(config.url).newBuilder().encodedPath("/ws/v1/node").build()
                report("正在连接中心服务")
                val ws = client.newWebSocket(Request.Builder().url(url).header("Authorization", "Bearer ${config.token}").build(), object: WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) { if (!events.trySend(Event(code=101)).isSuccess) webSocket.cancel() }
                    override fun onMessage(webSocket: WebSocket, text: String) {
                        if(text.toByteArray().size > MAX_MESSAGE) { webSocket.cancel(); return }
                        // Network reads run synchronously in the sole executor. Deliver cancellation
                        // immediately from this authenticated socket; persist it on the executor next.
                        val active=activeTransfer
                        val phone=activePhone
                        if(active!=null || phone!=null) runCatching {
                            val incoming=parseEnvelope(text);val p=incoming.payload
                            if(incoming.type=="command.cancel" && p.keys==setOf("session_id","attempt_id","reason")) {
                                if(active!=null && p["session_id"]==str(active.first) && p["attempt_id"]==str(active.second)) active.third.cancel()
                                if(phone!=null && p["session_id"]==str(phone.first) && p["attempt_id"]==str(phone.second)) phone.third.cancel()
                            }
                        }
                        if(!events.trySend(Event(text=text)).isSuccess) webSocket.cancel()
                    }
                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { events.trySend(Event(code=code)); webSocket.close(code,null) }
                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) { activePhone?.third?.cancel();events.trySend(Event(code=response?.code ?: 1006)); events.close() }
                })
                socket = ws
                messageLimit = MAX_MESSAGE
                try {
                    val opened = withTimeout(6000) { events.receive() }
                    if (opened.code != 101) {
                        if (opened.code in setOf(401,403)) { report("设备认证失败，请检查或重新登记设备身份"); return }
                        throw IllegalStateException("connect")
                    }
                    val notificationEnabled = config.packages.isNotEmpty() && NotificationAccess.connected
                    val phoneTools = access.tools()
                    val registered = (if(notificationEnabled) listOf(DEVICE,NOTIFICATIONS) else listOf(DEVICE)) + (if(config.receiveFiles) listOf(RECEIVE) else emptyList()) + phoneTools
                    val roots = listOf("android:device") + (if(notificationEnabled) listOf("android:notifications") else emptyList()) +
                        (if(config.receiveFiles) listOf("workspace") else emptyList()) + (if(UI_OBSERVE in phoneTools) listOf("android:ui") else emptyList()) + (if(FILES in phoneTools) listOf("android:files") else emptyList())
                    send("node.hello", obj("node_id" to str(config.nodeId), "node_version" to str("android-${ai.nexus.android.BuildConfig.VERSION_NAME}"), "boot_id" to str(boot),
                        "tools" to JsonArray(registered.map { obj("tool" to str(it), "tool_version" to JsonPrimitive(1)) }),
                        "roots" to JsonArray(roots.map(::str))))
                    val hello = withTimeout(5000) { parseEnvelope(requireNotNull(events.receive().text)) }
                    require(hello.type == "node.welcome")
                    val welcome = hello.payload
                    require(welcome.keys == setOf("session_id","heartbeat_interval_seconds","heartbeat_timeout_seconds","limits"))
                    val session = welcome.getValue("session_id").jsonPrimitive.content.also { UUID.fromString(it) }
                    val heartbeat = welcome.getValue("heartbeat_interval_seconds").jsonPrimitive.long.also { require(it in 1..10) } * 1000
                    require(welcome.getValue("heartbeat_timeout_seconds").jsonPrimitive.long in (heartbeat/1000+1)..30)
                    val limits = welcome.getValue("limits").jsonObject
                    require(limits.keys == setOf("max_result_bytes","max_message_bytes","max_concurrent_attempts","execution_timeout_seconds"))
                    val maxResult = limits.getValue("max_result_bytes").jsonPrimitive.int.also { require(it in 1..MAX_RESULT) }
                    val maxMessage = limits.getValue("max_message_bytes").jsonPrimitive.int.also { require(it in (maxResult+1)..MAX_MESSAGE) }
                    messageLimit = maxMessage
                    require(limits.getValue("max_concurrent_attempts").jsonPrimitive.int == 1)
                    val maxTime = limits.getValue("execution_timeout_seconds").jsonPrimitive.int.also { require(it in 1..10) }
                    serverTime = Instant.parse(hello.sent_at); clockStart = SystemClock.elapsedRealtime()
                    report("设备已连接 · ${registered.size} 项本地能力（实际权限以 Core 为准）")
                    delaySeconds = 1
                    var nextHeartbeat = 0L
                    var nextReplay = 0L
                    var replayCursor = ""
                    val replayCounts = mutableMapOf<String,Int>()
                    fun status(attempt: String, correlation: String? = null) {
                        val payload = store.status(attempt,session)
                        val saved = payload["result"] as? JsonObject
                        require(saved?.get("result")?.toString()?.toByteArray()?.size?.let { it <= maxResult } != false)
                        send("command.status_result",payload,correlation)
                    }
                    while (!stopping) {
                        val clock = SystemClock.elapsedRealtime()
                        if (notificationEnabled != (config.packages.isNotEmpty() && NotificationAccess.connected)) break // Re-register reduced/increased local capabilities.
                        if(phoneTools != access.tools()) break
                        if (clock >= nextHeartbeat) { send("node.heartbeat",obj("session_id" to str(session),"active_attempt_id" to JsonNull)); nextHeartbeat = clock+heartbeat }
                        if (clock >= nextReplay) {
                            val pending = store.pending(replayCursor)
                            for(record in pending) if ((replayCounts[record.attempt] ?: 0) < 3) { status(record.attempt); replayCounts[record.attempt] = (replayCounts[record.attempt] ?: 0)+1 }
                            replayCursor = if(pending.size == 4) pending.last().attempt else ""
                            nextReplay = clock+5000
                        }
                        val received = withTimeoutOrNull(250) { events.receiveCatching() } ?: continue
                        val event = received.getOrNull() ?: break
                        if(event.code != null) {
                            if(event.code == 1008) { report("设备连接被拒绝，请检查身份、协议和授权配置"); return }
                            break
                        }
                        require(event.text!!.toByteArray().size <= maxMessage)
                        val incoming = parseEnvelope(event.text)
                        val payload = incoming.payload
                        require(payload["session_id"]?.jsonPrimitive?.content == session)
                        val attempt = payload["attempt_id"]?.jsonPrimitive?.content
                        if(attempt != null) UUID.fromString(attempt)
                        when(incoming.type) {
                            "command.execute" -> {
                                val command = parseExecute(payload,config.nodeId,session)
                                if(command.tool !in registered) { send("error",obj("session_id" to str(session),"code" to str("POLICY_DENIED"),"message" to str("Android capability unavailable"),"retryable" to JsonPrimitive(false)),incoming.message_id); continue }
                                if(!store.receive(command)) { status(command.attempt_id,incoming.message_id); continue }
                                val receipt = obj("session_id" to str(session),"task_id" to str(command.task_id),"attempt_id" to str(command.attempt_id),"command_digest" to str(command.command_digest),"boot_id" to str(boot))
                                send("command.accepted",receipt,incoming.message_id)
                                val deadline = Instant.parse(command.expires_at)
                                var result: JsonObject? = null
                                var error: JsonObject? = null
                                val start = SystemClock.elapsedRealtime()
                                val executing=AtomicBoolean(false)
                                var deadlineWatch: java.util.concurrent.ScheduledFuture<*>?=null
                                try {
                                    require(now().isBefore(deadline) && command.execution_timeout_seconds <= maxTime)
                                    store.phase(command.attempt_id,"started")
                                    send("command.started",receipt,incoming.message_id)
                                    executing.set(true)
                                    deadlineWatch=watchdog.schedule({
                                        // A blocked Android Binder/storage call cannot be safely interrupted.
                                        // Terminate only this dedicated execution process; its durable receipt
                                        // becomes unknown on restart, and the UI/voice process stays alive.
                                        if(executing.compareAndSet(true,false)) android.os.Process.killProcess(android.os.Process.myPid())
                                    },command.execution_timeout_seconds.toLong(),TimeUnit.SECONDS)
                                    result = when(command.tool) {
                                        DEVICE -> tools.status()
                                        in PHONE_TOOLS -> {
                                            val operation=PhoneOperation { !stopping && now().isBefore(deadline) && access.enabled(if(command.tool in setOf(UI_OBSERVE,UI_ACT)+SCREEN_TOOLS) "accessibility" else "files") && (command.tool !in SCREEN_TOOLS || access.enabled("vision")) }
                                            activePhone=Triple(session,command.attempt_id,operation)
                                            operation.check()
                                            val arguments=phoneArguments(command.tool,command.arguments)
                                            when(command.tool) {
                                                UI_OBSERVE,UI_ACT,SCREEN_OBSERVE,SCREEN_ACT -> PhoneAccessibility.execute(command.tool,arguments,operation)
                                                FILES -> PhoneDocuments(context,operation).query(arguments)
                                                FILE_PREVIEW -> PhoneDocuments(context,operation).preview(arguments)
                                                else -> PhoneDocuments(context,operation).write(arguments)
                                            }
                                        }
                                        RECEIVE -> {
                                            val receiver=FileReceiver(context,config,client)
                                            activeTransfer=Triple(session,command.attempt_id,receiver)
                                            if(stopping) receiver.cancel()
                                            receiver.receive(command)
                                        }
                                        NOTIFICATIONS -> NotificationAccess.digest(command.arguments["limit"]?.jsonPrimitive?.int ?: 10)
                                        else -> error("Unsupported tool")
                                    }
                                    require(result.toString().toByteArray().size <= maxResult)
                                    require(now().isBefore(deadline) && SystemClock.elapsedRealtime()-start < command.execution_timeout_seconds*1000)
                                } catch (unknown: PhoneEffectUnknown) {
                                    report(unknown.message ?: "操作结果待核对")
                                    store.phase(command.attempt_id,"unknown");status(command.attempt_id,incoming.message_id);continue
                                } catch (cancelled: ReceiveCancelled) {
                                    store.phase(command.attempt_id,"cancelled");status(command.attempt_id,incoming.message_id);continue
                                } catch (failure: android.database.SQLException) { throw failure }
                                catch (failure: Exception) {
                                    if(command.tool in setOf(UI_ACT,SCREEN_ACT,FILE_WRITE) && result!=null) {
                                        store.phase(command.attempt_id,"unknown");status(command.attempt_id,incoming.message_id);continue
                                    }
                                    result = null
                                    val detail=if(command.tool in PHONE_TOOLS && failure is IllegalArgumentException || command.tool in PHONE_TOOLS && failure is IllegalStateException) failure.message?.take(300) else null
                                    error = obj("code" to str("POLICY_DENIED"),"message" to str(detail ?: "设备操作未完成，请核对权限、连接、空间及执行记录；文件不会自动重发。"),"retryable" to JsonPrimitive(false))
                                } finally { activeTransfer=null;activePhone=null;executing.set(false);deadlineWatch?.cancel(false) }
                                val resultPayload = obj("session_id" to str(session),"task_id" to str(command.task_id),"attempt_id" to str(command.attempt_id),
                                    "command_digest" to str(command.command_digest),"tool" to str(command.tool),"tool_version" to JsonPrimitive(1),"observed_at" to str(utc(now())),
                                    "outcome" to str(if(result != null) "succeeded" else "failed"),"result" to (result ?: JsonNull),"error" to (error ?: JsonNull))
                                store.phase(command.attempt_id,"completed",resultPayload)
                                send("command.result",resultPayload,incoming.message_id)
                            }
                            "command.status" -> { require(payload.keys == setOf("session_id","attempt_id")); status(requireNotNull(attempt),incoming.message_id) }
                            "command.cancel" -> {
                                require(payload.keys == setOf("session_id","attempt_id","reason"))
                                store.cancel(requireNotNull(attempt))
                                if(store.get(attempt) == null) send("command.cancelled",obj("session_id" to str(session),"attempt_id" to str(attempt),"stopped_phase" to str("received")),incoming.message_id)
                                else status(attempt,incoming.message_id)
                            }
                            "result.ack" -> { require(payload.keys == setOf("session_id","attempt_id","status")); require(payload["status"]?.jsonPrimitive?.content in setOf("stored","already_settled")); store.ack(requireNotNull(attempt)) }
                            "error" -> { report("中心服务拒绝了设备消息，请核对配置"); return }
                            else -> error("Unexpected Core message")
                        }
                    }
                } catch (cancel: CancellationException) { throw cancel }
                catch (failure: android.database.SQLException) { report("回执保存失败，设备执行已停止"); return }
                catch (failure: Exception) { if(!stopping) report("连接中断，等待重连并核对原执行") }
                finally { ws.cancel(); events.close(); socket = null; store.interrupted() }
                if(!stopping) { delay(delaySeconds*1000 + Random.nextLong(250)); delaySeconds = (delaySeconds*2).coerceAtMost(30) }
            }
        } finally { NotificationAccess.configure(emptySet()); store.close(); watchdog.shutdownNow();client.dispatcher.executorService.shutdown(); client.connectionPool.evictAll() }
    }
}
