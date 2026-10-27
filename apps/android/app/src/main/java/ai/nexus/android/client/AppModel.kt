package ai.nexus.android.client

import android.app.Application
import android.content.Intent
import androidx.compose.runtime.*
import androidx.lifecycle.AndroidViewModel
import ai.nexus.android.node.NodeService
import ai.nexus.android.node.NodeConfig
import ai.nexus.android.voice.CoreVoice
import ai.nexus.android.voice.PcmMedia
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.*
import java.util.UUID

fun JsonObject.text(key: String): String = (this[key] as? JsonPrimitive)?.contentOrNull ?: ""
fun JsonObject.objectAt(key: String): JsonObject = this[key] as? JsonObject ?: JsonObject(emptyMap())
fun JsonObject.objects(key: String): List<JsonObject> = (this[key] as? JsonArray)?.mapNotNull { it as? JsonObject } ?: emptyList()
fun uid() = UUID.randomUUID().toString()

/** UI projections are disposable. Core remains the only source of task and chat state. */
class AppModel(application: Application) : AndroidViewModel(application) {
    val client = CoreClient(Vault(application,"owner"))
    private val enrollment = DeviceEnrollment(application,client)
    var localDevice by mutableStateOf(JsonObject(emptyMap())); private set
    var deviceSyncNotice by mutableStateOf("权限等待同步"); private set
    var deviceStartRequest by mutableIntStateOf(0); private set
    fun deviceStartHandled() { deviceStartRequest = 0 }
    suspend fun syncDevice() {
        val revision = sessionRevision
        try {
            val result = enrollment.sync()
            if(revision != sessionRevision || !signedIn) return
            localDevice = result.status
            if(result.installed) deviceStartRequest++
            if(result.error != null) throw result.error
            val choices = localDevice.objectAt("permission_sync").objectAt("permissions")
            val different = choices.any { (key,value) -> localDevice.objectAt("permissions")[key] != value }
            deviceSyncNotice = when {
                localDevice.text("revoked") == "true" -> "设备身份已撤销，权限未同步。"
                localDevice.text("registered") != "true" -> "注册设备后，本机权限会自动同步。"
                different -> "本机选择已同步；部分权限已在 Core 另行调整，以当前许可为准。"
                else -> "本机权限已同步到 Core"
            }
        } catch(cancel: CancellationException) { throw cancel }
        catch(error: Exception) {
            if(revision != sessionRevision) return
            deviceSyncNotice = when((error as? ApiFailure)?.status) {
                401 -> "权限待同步：请重新登录。"
                403 -> "权限同步被拒绝，请核对设备身份与授权。"
                404 -> "权限待同步：Core 需要更新以支持自动同步。"
                409 -> "权限版本冲突，尚未同步；请核对本机与 Core 的设备配置。"
                else -> "权限待同步，连接恢复后自动重试；本机关闭立即生效。"
            }
            if(error is ApiFailure && error.status == 401) report(error)
        }
    }
    fun syncDevicePermissions() {
        deviceSyncNotice = "权限待同步"
        if(signedIn) scope.launch { syncDevice() }
    }
    private val journal = Vault(application,"pending")
    private val draftStore = Vault(application,"draft")
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var polling: Job? = null
    private var nodePolling: Job? = null
    private var devicePolling: Job? = null
    private var activityPolling: Job? = null
    private var sessionRevision = 0
    private val updates = Mutex()
    private var inForeground = false
    var appForeground by mutableStateOf(false); private set
    private var voice: CoreVoice? = null
    private var voiceJob: Job? = null
    private var voiceSessionId = ""
    var voiceState by mutableStateOf("stopped"); private set
    var voiceDetail by mutableStateOf(""); private set
    var voiceInfo by mutableStateOf(JsonObject(emptyMap())); private set
    var voiceDialog by mutableStateOf(false)
    var voiceTools by mutableStateOf(false)
    val voiceActive get() = voiceState !in setOf("stopped","error")
    var signedIn by mutableStateOf(false); private set
    var busy by mutableStateOf(false); private set
    var notice by mutableStateOf("")
    var assistantSettings by mutableStateOf<JsonObject?>(null); private set
    var assistantSettingsNotice by mutableStateOf(""); private set
    var assistantSettingsStale by mutableStateOf(false); private set
    var syncState by mutableStateOf("正在同步"); private set
    var page by mutableIntStateOf(0)
    var conversations by mutableStateOf(emptyList<JsonObject>())
    var archived by mutableStateOf(false)
    var history by mutableStateOf(JsonObject(emptyMap()))
    var olderTurns by mutableStateOf(emptyList<JsonObject>())
    var nodes by mutableStateOf(emptyList<JsonObject>())
    var nodesFresh by mutableStateOf(false); private set
    var activityFresh by mutableStateOf(false); private set
    var sceneTasks by mutableStateOf(emptyList<JsonObject>())
    var activity by mutableStateOf<JsonObject?>(null); private set
    var control by mutableStateOf(JsonObject(emptyMap())); private set
    var catalog by mutableStateOf(JsonObject(emptyMap()))
    var tasks by mutableStateOf(emptyList<JsonObject>())
    var taskDetail by mutableStateOf(JsonObject(emptyMap()))
    private val uploadJournal = Vault(application,"file-upload")
    var fileUpload by mutableStateOf<JsonObject?>(null); private set
    private var tasksCursor = ""
    var tasksMore by mutableStateOf(false)
    private val conversationDrafts = mutableMapOf<String,String>()
    private var sentRouteRequest = ""
    private val consumedNavigation = mutableSetOf<String>()
    var draft by mutableStateOf("")
    var allowModel by mutableStateOf(false)
    var allowContext by mutableStateOf(true)
    var allowTools by mutableStateOf(true)
    var allowResults by mutableStateOf(false)
    var pending by mutableStateOf<JsonObject?>(null); private set
    val conversationId get() = history.objectAt("conversation").text("conversation_id")
    val activeTurn get() = history.text("active_turn_id")
    val sendBlockedReason get() = when {
        busy -> "正在处理，请稍候"
        pending != null -> "请先核对上一次操作"
        voiceActive -> "请先结束语音"
        activeTurn.isNotEmpty() -> "正在回复，可先停止当前工作"
        history.objectAt("conversation").text("archived") == "1" -> "请先恢复归档会话"
        else -> ""
    }

    init {
        runCatching {
            pending = journal.read()
            fileUpload = uploadJournal.read()
            val saved = draftStore.read()
            draft = saved?.text("text") ?: ""
            saved?.objectAt("conversations")?.forEach { (id,value) -> conversationDrafts[id] = value.jsonPrimitive.content }
            conversationDrafts.putIfAbsent("",draft)
            signedIn = client.restore()
        }.onFailure { notice = "本机加密数据暂时无法读取，请重新登录；设备回执不会被重置。" }
        if(signedIn) action { client.checkSession(); refresh() }
    }
    fun editDraft(value: String) { draft = value.take(2000) }
    fun saveDraft() { conversationDrafts[conversationId] = draft; runCatching { draftStore.write(obj("text" to str(draft), "conversations" to JsonObject(conversationDrafts.mapValues { str(it.value) }))) }.onFailure { notice = "草稿未能保存，请保留当前文字。" } }
    fun foreground(active: Boolean) {
        appForeground = active
        inForeground = active
        polling?.cancel(); polling = null
        nodePolling?.cancel(); nodePolling = null
        devicePolling?.cancel(); devicePolling = null
        activityPolling?.cancel(); activityPolling = null
        nodesFresh = false; activityFresh = false
        if(!active) { stopVoice(); saveDraft(); return }
        // Owner-authenticated permission sync is independent of chat work and Node credentials.
        devicePolling = scope.launch {
            while(isActive) {
                if(signedIn) syncDevice()
                delay(5000)
            }
        }
        // Device and activity reads must not wait behind a send, file read or chat history.
        nodePolling = scope.launch {
            while(isActive) {
                if(signedIn) {
                    val revision = sessionRevision
                    try {
                        val value = withTimeoutOrNull(8000) { client.request("GET","/api/v1/nodes").objects("nodes") }
                        if(signedIn && revision == sessionRevision) { if(value != null) nodes = value; nodesFresh = value != null }
                    } catch(cancel: CancellationException) { throw cancel }
                    catch(error: Exception) {
                        if(revision == sessionRevision) { nodesFresh = false; if(error is ApiFailure && error.status == 401) report(error) }
                    }
                }
                delay(2000)
            }
        }
        activityPolling = scope.launch {
            while(isActive) {
                if(signedIn) {
                    val revision = sessionRevision
                    try {
                        val synced = withTimeoutOrNull(8000) {
                            val value = client.request("GET","/api/v1/client/snapshot")
                            if(signedIn && revision == sessionRevision) applySnapshot(value)
                            true
                        }
                        if(synced == null) activityFresh = false
                    } catch(cancel: CancellationException) { throw cancel }
                    catch(error: Exception) {
                        if(revision == sessionRevision) { activityFresh = false; if(error is ApiFailure && error.status == 401) report(error) }
                    }
                }
                delay(2000)
            }
        }
        polling = scope.launch {
            var retry = 1500L
            while(isActive) {
                if(signedIn && !busy) try {
                    updates.withLock {
                        if(signedIn && !busy) {
                            refresh()
                            if(page == 1 && taskDetail.objectAt("task").text("task_id").isNotEmpty()) refreshTask()
                        }
                    }
                    syncState = "已同步"; retry = 2500L
                } catch(cancel: CancellationException) { throw cancel }
                catch(error: Exception) {
                    syncState = "连接中断，正在重连"
                    retry = (retry * 2).coerceAtMost(30000L)
                    if(error is ApiFailure && error.status == 401) report(error)
                }
                delay(retry)
            }
        }
    }
    fun action(block: suspend () -> Unit) {
        if(busy) return
        busy = true; notice = ""
        scope.launch {
            try { updates.withLock { block() } } catch(cancel: CancellationException) { throw cancel }
            catch(error: Exception) { report(error) }
            finally { busy = false }
        }
    }
    private fun report(error: Exception) {
        notice = when(error) {
            is ApiFailure -> error.message ?: "请求失败"
            is IllegalArgumentException -> error.message ?: "输入不符合要求，请核对后重试。"
            else -> "暂时无法连接中心服务。正在自动核对已发送的操作，请勿重复创建。"
        }
        if(error is ApiFailure && error.status == 409) {
            catalog = JsonObject(emptyMap())
            notice = "状态已更新，正在自动同步。请核对后重新发送。"
        }
        if(error is ApiFailure && error.status == 401) {
            assistantSettings = null
            sessionRevision++; nodesFresh = false; activityFresh = false; sceneTasks = emptyList(); activity = null
            signedIn = false; history = JsonObject(emptyMap()); conversations = emptyList(); tasks = emptyList(); taskDetail = JsonObject(emptyMap())
        }
    }
    fun login(url: String, token: String) = action { sessionRevision++; client.login(url,token.trim()); signedIn = true; foreground(inForeground); refresh() }
    fun logout() = action {
        val app=getApplication<Application>()
        app.getSharedPreferences("reminder-status",0).edit().putBoolean("enabled",false).apply()
        app.stopService(Intent(app,ai.nexus.android.reminders.ReminderService::class.java))
        val reminders=Vault(app,"reminders")
        reminders.read()?.text("client_id")?.takeIf { it.isNotEmpty() }?.let { id ->
            try { client.request("DELETE","/api/v1/reminders/clients/$id") } catch(_:Exception) { /* local stop is unconditional */ }
        }
        reminders.clear()
        sessionRevision++; nodePolling?.cancel(); activityPolling?.cancel(); devicePolling?.cancel()
        stopVoice()
        getApplication<Application>().startService(Intent(getApplication(),NodeService::class.java).setAction("STOP"))
        try { client.logout() } finally {
            assistantSettings = null; assistantSettingsNotice = ""
            signedIn = false; conversations = emptyList(); history = JsonObject(emptyMap()); olderTurns = emptyList()
            localDevice = JsonObject(emptyMap()); deviceSyncNotice = "登录后同步本机权限"
            nodes = emptyList(); tasks = emptyList(); taskDetail = JsonObject(emptyMap()); catalog = JsonObject(emptyMap())
            sceneTasks = emptyList(); activity = null; control = JsonObject(emptyMap()); nodesFresh = false; activityFresh = false; tasksCursor = ""
            conversationDrafts.clear(); consumedNavigation.clear(); sentRouteRequest = ""
            draft = ""; draftStore.clear(); allowModel = false; allowTools = false; allowResults = false
        }
    }
    suspend fun refresh() {
        conversations = client.request("GET","/api/v1/chat/conversations?archived=$archived").objects("conversations")
        if(catalog.isEmpty()) catalog = client.request("GET","/api/v1/chat/work-catalog")
        if(conversationId.isNotEmpty()) refreshHistory()
    }
    private suspend fun performFileUpload(saved: JsonObject) {
        require(saved.text("url")==client.base.toString()) { "请回到原 Core 核对上传。" }
        val bytes=withContext(Dispatchers.IO) {
            requireNotNull(getApplication<Application>().contentResolver.openInputStream(android.net.Uri.parse(saved.text("uri")))).use { input ->
                val output=java.io.ByteArrayOutputStream();val buffer=ByteArray(65536)
                while(true) { val n=input.read(buffer);if(n<0)break;require(output.size()+n<=16*1024*1024){"单个文件不能超过 16 MiB。"};output.write(buffer,0,n) }
                output.toByteArray()
            }
        }
        val name=java.net.URLEncoder.encode(saved.text("filename"),"UTF-8")
        client.request("POST","/api/v1/chat/conversations/${saved.text("conversation_id")}/attachments?request_id=${saved.text("request_id")}&filename=$name",raw=bytes)
        uploadJournal.clear();fileUpload=null
        notice="旧版上传已核对，文件保留在原会话中。"
    }
    fun retryFileUpload() = action { performFileUpload(requireNotNull(fileUpload)) }
    fun dismissFileUpload() { uploadJournal.clear();fileUpload=null;notice="已清除本机上传标记；Core 已存储的文件仍保留，可在原会话核对。" }
    fun refreshAll() = action { refresh() }
    fun loadAssistantSettings() = action {
        assistantSettings = null; assistantSettingsNotice = "正在读取表达偏好…"
        try {
            assistantSettings = client.request("GET","/api/v1/assistant/preferences")
            assistantSettingsNotice = ""; assistantSettingsStale = false
        } catch(error: java.io.IOException) {
            val status=(error as? ApiFailure)?.status
            assistantSettingsNotice = if(status == 404) "当前服务暂不支持人格设置，更新后即可使用。" else "暂时无法读取表达偏好，请重试。"
            if(status == 401) throw error
        }
    }
    fun saveAssistantSettings(personality: String, emoji: String, revision: JsonElement) = action {
        assistantSettingsNotice = "正在保存…"
        try {
            val result = client.request("PUT","/api/v1/assistant/preferences",obj("personality" to str(personality),"emoji" to str(emoji),"expected_revision" to revision))
            assistantSettings = JsonObject(requireNotNull(assistantSettings) + ("preferences" to result.getValue("preferences")))
            assistantSettingsNotice = "已保存，从下一轮对话生效。"
        } catch(error: java.io.IOException) {
            val status=(error as? ApiFailure)?.status
            assistantSettingsStale = status == 409
            assistantSettingsNotice = if(assistantSettingsStale) "设置已在其他设备更新，请读取最新设置后再修改。" else "尚未确认保存成功，请读取最新设置核对。"
            if(status == 401) throw error
        }
    }
    fun listConversations(showArchived: Boolean) = action {
        archived = showArchived
        conversations = client.request("GET","/api/v1/chat/conversations?archived=$archived").objects("conversations")
    }
    fun moreConversations() = action {
        val cursor = conversations.lastOrNull()?.text("cursor") ?: return@action
        conversations = (conversations + client.request("GET","/api/v1/chat/conversations?archived=$archived&before=$cursor").objects("conversations")).distinctBy { it.text("conversation_id") }
    }
    fun selectConversation(id: String, turnId: String = "") = action { openConversationSource(id,turnId) }
    private suspend fun openConversationSource(id: String, turnId: String = "") {
        val updated = client.request("GET","/api/v1/chat/conversations/$id/turns")
        val source = if(turnId.isNotEmpty() && updated.objects("turns").none { it.text("turn_id")==turnId })
            client.request("GET","/api/v1/chat/turns/$turnId").objectAt("turn") else null
        saveDraft(); stopVoice()
        olderTurns = if(source != null && source.text("conversation_id")==id) listOf(source) else emptyList()
        history = updated; draft = conversationDrafts[id] ?: ""; saveDraft()
        allowResults = false; page = 0
    }
    private suspend fun refreshHistory() {
        val id = conversationId
        if(id.isNotEmpty()) {
            val updated = client.request("GET","/api/v1/chat/conversations/$id/turns")
            if(conversationId == id) {
                history = updated
                val turn = updated.objects("turns").firstOrNull { it.text("request_id")==sentRouteRequest && it.objectAt("presentation").text("phase")=="completed" }
                val navigation = turn?.objectAt("presentation")?.objectAt("navigation")
                val step = navigation?.text("step_id") ?: ""
                if(step.isNotEmpty() && consumedNavigation.add(step)) {
                    runCatching { openConversationSource(navigation!!.text("conversation_id")) }
                        .onFailure { notice = "会话切换失败，可点击回复中的来源重试。" }
                }
            }
        }
    }
    fun earlierTurns() = action {
        val cursor = (olderTurns + history.objects("turns")).firstOrNull()?.text("cursor") ?: return@action
        olderTurns = (client.request("GET","/api/v1/chat/conversations/$conversationId/turns?before=$cursor").objects("turns") + olderTurns).distinctBy { it.text("turn_id") }
    }
    private suspend fun createConversation() {
        val result = write("POST","/api/v1/chat/conversations",obj("request_id" to str(uid())),true)
        // Keep the committed identity even if a later read or send fails.
        val conversation = result.objectAt("conversation")
        olderTurns = emptyList(); history = obj("conversation" to conversation)
        conversations = listOf(conversation) + conversations
    }
    fun newConversation() = action {
        saveDraft(); stopVoice(); createConversation(); draft = ""; saveDraft()
    }
    fun updateConversation(title: String? = null, archive: Boolean? = null) = action {
        val data = buildJsonObject { title?.let { put("title",it.trim()) }; archive?.let { put("archived",it) } }
        write("PATCH","/api/v1/chat/conversations/$conversationId",data,false)
        if(archive == true) { history = JsonObject(emptyMap()); olderTurns = emptyList() }
        refresh()
    }
    fun renameNode(id: String, name: String) = action {
        write("PATCH","/api/v1/nodes/$id/name",obj("name" to str(name.trim())),false)
        catalog = client.request("GET","/api/v1/chat/work-catalog"); refresh()
    }
    fun send() = action {
        require(!voiceActive) { "请先结束语音，再发送文字。" }
        require(allowModel) { "请先同意本轮向已配置的模型发送文字。" }
        require(draft.isNotBlank() && draft.toByteArray().size <= 6000) { "请输入不超过 2000 字且 6000 字节的内容。" }
        require(activeTurn.isEmpty()) { "请等待当前回复结束。" }
        require(history.objectAt("conversation").text("archived")!="1") { "请先恢复归档会话。" }
        require(!allowTools || allowResults) { "请先同意本轮工具结果外发。" }
        if(allowTools) catalog = client.request("GET","/api/v1/chat/work-catalog")
        val installation = enrollment.prepare()
        if(conversationId.isEmpty()) createConversation()
        val data = buildJsonObject {
            put("request_id",uid()); put("expected_revision",history.objectAt("conversation").text("revision").toInt())
            put("text",draft); put("allow_model",true); put("include_context",allowContext)
            put("allow_tools",allowTools)
            put("device_client_id",installation.text("client_id"))
            val local = Vault(getApplication(),"node").read()
            if(local != null && coreUrl(local.text("url"))==client.base) put("client_node_id",local.text("node_id"))
            if(allowTools) { put("all_nodes",true); put("workspace_work",true); put("work_digest",catalog.text("work_digest")); put("allow_work_results",true) }
        }
        saveDraft()
        write("POST","/api/v1/chat/conversations/$conversationId/turns",data,true)
        sentRouteRequest = data.text("request_id")
        draft = ""; saveDraft(); allowModel = false; allowResults = false
        refreshHistory()
    }
    fun stopTurn(id: String) = action { write("POST","/api/v1/chat/turns/$id/stop",obj(),false); refreshHistory() }
    fun answer(turn: JsonObject, step: JsonObject, text: String) = action {
        write("POST","/api/v1/chat/turns/${turn.text("turn_id")}/permission",obj("request_id" to str(uid()),"step_id" to str(step.text("step_id")),"expected_revision" to JsonPrimitive(turn.text("revision").toInt()),"text" to str(text)),true)
        refreshHistory()
    }
    private suspend fun applySnapshot(value: JsonObject) {
        val recent = value.objects("tasks")
        val current = value.objects("active_tasks") + recent
        val ids = current.map { it.text("task_id") }.toSet()
        val ended = sceneTasks.filter { it.text("status") !in setOf("succeeded","failed","cancelled","timed_out") && it.text("task_id") !in ids }
            .map { client.request("GET","/api/v1/tasks/${it.text("task_id")}").objectAt("task") }
        sceneTasks = (current + ended).distinctBy { it.text("task_id") }
        mergeTasks(sceneTasks)
        if(tasksCursor.isEmpty()) { tasksCursor = value.text("next_before"); tasksMore = value.text("has_more") == "true" }
        control = value.objectAt("control")
        activity = value["activity"] as? JsonObject
        activityFresh = true
    }
    private fun mergeTasks(incoming: List<JsonObject>) {
        tasks = (incoming + tasks).groupBy { it.text("task_id") }.values
            .map { versions -> versions.maxBy { it.text("revision").toIntOrNull() ?: 0 } }
            .sortedByDescending { it.text("created_at") }
    }
    suspend fun loadTasks(more: Boolean) {
        val query = if(more && tasksCursor.isNotEmpty()) "?before=$tasksCursor" else ""
        val result = client.request("GET","/api/v1/client/snapshot$query")
        mergeTasks(result.objects("tasks") + result.objects("active_tasks"))
        tasksCursor = result.text("next_before"); tasksMore = result.text("has_more") == "true"
    }
    fun taskPage(more: Boolean = false) = action { loadTasks(more) }
    fun viewTask(id: String) = action { taskDetail = client.request("GET","/api/v1/tasks/$id") }
    private suspend fun refreshTask() {
        val id = taskDetail.objectAt("task").text("task_id")
        val updated = client.request("GET","/api/v1/tasks/$id")
        if(taskDetail.objectAt("task").text("task_id") == id) taskDetail = updated
    }
    fun decide(approval: String, decision: String) = action {
        write("POST","/api/v1/approvals/$approval",obj("decision" to str(decision)),false); refreshTask(); loadTasks(false)
    }
    fun cancelTask() = action { write("POST","/api/v1/tasks/${taskDetail.objectAt("task").text("task_id")}/cancel",obj(),false); refreshTask() }
    fun deviceStatus(id: String) = action {
        val result = write("POST","/api/v1/tasks",obj("node_id" to str(id),"request" to obj("request_id" to str(uid()),"intent" to str("android_device_status"),"arguments" to obj())),true)
        page = 1; taskDetail = client.request("GET","/api/v1/tasks/${result.objectAt("task").text("task_id")}"); loadTasks(false)
    }
    /** Persist the exact write before sending. Only an explicit retry can replay its original ID/body. */
    private suspend fun write(method: String, path: String, data: JsonObject, retryable: Boolean): JsonObject {
        check(pending == null) { "请先核对上一次操作。" }
        val saved = obj("url" to str(client.base.toString()),"method" to str(method),"path" to str(path),"body" to data,"retryable" to JsonPrimitive(retryable))
        journal.write(saved); pending = saved
        return performPending(saved)
    }
    private suspend fun performPending(saved: JsonObject): JsonObject {
        try {
            val response = client.request(saved.text("method"),saved.text("path"),saved.objectAt("body"),proof=enrollment.proofFor(saved.objectAt("body").text("device_client_id")))
            journal.clear(); pending = null
            if(response.objectAt("turn").objectAt("device_change").text("changed") == "true") {
                syncDevice()
                if(enrollment.startFor(response.objectAt("turn"))) deviceStartRequest++
                catalog = JsonObject(emptyMap())
            }
            return response
        } catch(error: ApiFailure) {
            if(error.status in setOf(400,401,403,404,409,413,422,429)) { journal.clear(); pending = null }
            throw error
        }
    }
    fun retryPending() = action {
        val saved = requireNotNull(pending)
        require(saved.text("url") == client.base.toString() && saved.text("retryable") == "true")
        val result = performPending(saved)
        if(saved.text("path") == "/api/v1/chat/conversations") {
            history = client.request("GET","/api/v1/chat/conversations/${result.objectAt("conversation").text("conversation_id")}/turns")
        }
        if(saved.text("path").endsWith("/turns")) { draft = ""; saveDraft() }
        allowModel = false; allowTools = false; allowResults = false; refresh()
    }
    fun acknowledgePending() { journal.clear(); pending = null; notice = "已清除本地核对标记。服务端任务状态以查询结果为准。" }
    fun showVoice() = action {
        require(conversationId.isNotEmpty() && activeTurn.isEmpty()) { "请先打开会话，并等待当前回复结束。" }
        val local = NodeConfig.from(requireNotNull(Vault(getApplication(),"node").read()) { "请先在设备页登记本机 Node 身份。" })
        require(coreUrl(local.url)==client.base) { "本机 Node 与聊天必须连接同一个 Core。" }
        voiceInfo = client.request("GET","/api/v1/voice")
        require(voiceInfo.text("enabled")=="true" && (voiceInfo["nodes"] as? JsonArray)?.any{it.jsonPrimitive.content==local.nodeId}==true) { "Core 尚未为这台手机启用语音，请按接入说明配置。" }
        voiceTools=false;voiceDialog=true
    }
    fun startVoice() = action {
        require(inForeground && !voiceActive && voiceJob?.isActive!=true && activeTurn.isEmpty() && pending==null)
        val config=NodeConfig.from(requireNotNull(Vault(getApplication(),"node").read()))
        require(coreUrl(config.url)==client.base)
        voiceState="activating";voiceDetail="正在建立 Core 语音会话"
        val request=uid()
        try {
            val prepared=write("POST","/api/v1/voice/sessions",obj("request_id" to str(request),"node_id" to str(config.nodeId),"conversation_id" to str(conversationId),
                "allow_audio_model" to JsonPrimitive(true),"allow_model" to JsonPrimitive(true),"include_context" to JsonPrimitive(allowContext),
                "allow_tools" to JsonPrimitive(voiceTools),"allow_work_results" to JsonPrimitive(voiceTools),"trigger" to str("button")),true)
            voiceSessionId=prepared.text("voice_session_id")
            if(!inForeground || voiceState=="stopped") { stopVoice();return@action }
            val media=PcmMedia(getApplication()) { stopVoice() }
            val runtime=CoreVoice(config,request,voiceSessionId,conversationId,media) { state,detail -> scope.launch {
                voiceState=state;voiceDetail=detail
            } }
            voice=runtime
            voiceJob=scope.launch(Dispatchers.IO) {
                try { runtime.run() }
                catch(cancel: CancellationException) { throw cancel }
                catch(error: Exception) { withContext(Dispatchers.Main) { voiceState="error";voiceDetail="语音连接已中断，麦克风和播放已关闭。";notice=voiceDetail } }
                finally { withContext(NonCancellable+Dispatchers.Main) { if(voice===runtime) { voice=null; if(voiceState !in setOf("error","stopped")) voiceState="stopped" };reconcileVoice() } }
            }
        } catch(error: Exception) { voiceState="error";voiceDetail="未开启麦克风。请核对待处理会话后再试。";throw error }
    }
    fun interruptVoice() { voice?.interrupt() }
    fun stopVoice() {
        voice?.stop();voiceState="stopped";voiceDialog=false
        if(voiceSessionId.isNotEmpty()) scope.launch { reconcileVoice() }
    }
    private suspend fun reconcileVoice() {
        val id=voiceSessionId
        if(id.isEmpty()) return
        voiceSessionId=""
        try { client.request("POST","/api/v1/voice/sessions/$id/stop",obj()) }
        catch(error: Exception) { notice="本机麦克风和播放已关闭；Core 断线状态由服务端核对，请刷新语音状态。" }
    }
    override fun onCleared() { voice?.stop();scope.cancel(); client.http.dispatcher.cancelAll(); super.onCleared() }
}
