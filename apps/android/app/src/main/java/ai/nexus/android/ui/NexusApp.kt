package ai.nexus.android.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.text.style.TextOverflow
import ai.nexus.android.BuildConfig
import androidx.compose.ui.Modifier
import androidx.compose.ui.Alignment
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import ai.nexus.android.client.*
import ai.nexus.android.node.NodeConfig
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.serialization.json.*

// Shared visual tokens from apps/windows/ui/app.css (DSH): monochrome, 14px panels.
private val colors = darkColorScheme(primary=Color.White,onPrimary=Color.Black,
    background=Color.Black,surface=Color(0xFF0D0D0D),surfaceVariant=Color(0xFF181818),
    surfaceContainer=Color(0xFF121212),surfaceContainerHigh=Color(0xFF181818),
    onSurface=Color(0xFFF2F2F2),onBackground=Color(0xFFF2F2F2),
    secondary=Color(0xFFA3A3A3),onSecondary=Color.Black,secondaryContainer=Color(0xFF242424),
    outline=Color(0xFF333333),outlineVariant=Color(0xFF242424),error=Color(0xFFE0E0E0))
private val pretty = Json { prettyPrint = true }
private fun formatted(value: JsonElement) = pretty.encodeToString(JsonElement.serializer(),value).take(18000)

@Composable fun NexusApp(model: AppModel, nodeState: String, nodeRunning: Boolean, onVoiceStart: () -> Unit, onStartNode: () -> Unit, onStopNode: () -> Unit, onNotificationSettings: () -> Unit) {
    MaterialTheme(colorScheme=colors, shapes=Shapes(small=RoundedCornerShape(9.dp),medium=RoundedCornerShape(14.dp),large=RoundedCornerShape(20.dp))) {
        Surface(Modifier.fillMaxSize()) {
            if(!model.signedIn) Login(model) else BoxWithConstraints(Modifier.fillMaxSize()) {
                val rail = maxWidth >= 600.dp
                Scaffold(
                    topBar = { Row(Modifier.statusBarsPadding().padding(horizontal=20.dp,vertical=10.dp),verticalAlignment=Alignment.CenterVertically) {
                        Box(Modifier.size(32.dp).border(1.dp,Color(0xFF4A4A4A),RoundedCornerShape(9.dp)),contentAlignment=Alignment.Center) { Text("N",fontWeight=FontWeight.SemiBold) }
                        Spacer(Modifier.width(10.dp))
                        Text("NEXUS",style=MaterialTheme.typography.titleMedium,fontWeight=FontWeight.SemiBold,modifier=Modifier.weight(1f))
                        Text(if(model.syncState=="已同步") "${model.nodes.count{it.text("online")=="true"}} 台设备在线" else model.syncState,style=MaterialTheme.typography.labelSmall,color=MaterialTheme.colorScheme.secondary)
                    } },
                    bottomBar = { if(!rail && WindowInsets.ime.getBottom(androidx.compose.ui.platform.LocalDensity.current)==0) NavigationBar {
                        listOf("对话","任务","设备","设置").forEachIndexed { index,title ->
                            NavigationBarItem(selected=model.page==index,onClick={model.page=index;if(index==1)model.taskPage()},icon={Text(listOf("◈","≡","▣","⚙")[index])},label={Text(title)})
                        }
                    } }
                ) { padding -> Row(Modifier.padding(padding).imePadding().fillMaxSize()) {
                    if(rail) NavigationRail {
                        listOf("对话","任务","设备","设置").forEachIndexed { index,title ->
                            NavigationRailItem(selected=model.page==index,onClick={model.page=index;if(index==1)model.taskPage()},icon={Text(listOf("◈","≡","▣","⚙")[index])},label={Text(title)})
                        }
                    }
                    Column(Modifier.weight(1f).fillMaxHeight()) {
                        Notice(model)
                        if(model.pending!=null) PendingPanel(model)
                        when(model.page) {
                            0 -> ChatPage(model)
                            1 -> Box(Modifier.fillMaxSize(),contentAlignment=Alignment.TopCenter) { Box(Modifier.widthIn(max=900.dp).fillMaxSize()){TasksPage(model)} }
                            2 -> Box(Modifier.fillMaxSize(),contentAlignment=Alignment.TopCenter) { Box(Modifier.widthIn(max=760.dp).fillMaxSize()){DevicesPage(model,nodeState,nodeRunning,onStartNode,onStopNode,onNotificationSettings)} }
                            else -> Box(Modifier.fillMaxSize(),contentAlignment=Alignment.TopCenter) { Box(Modifier.widthIn(max=760.dp).fillMaxSize()){SettingsPage(model,onStopNode)} }
                        }
                    }
                } }
            }
            if(model.voiceDialog) VoiceDialog(model,onVoiceStart)
        }
    }
}

@Composable private fun Login(model: AppModel) {
    var url by remember { mutableStateOf(model.client.base?.toString() ?: "") }
    var token by remember { mutableStateOf("") }
    Box(Modifier.fillMaxSize(),contentAlignment=Alignment.Center) { Column(Modifier.widthIn(max=480.dp).safeDrawingPadding().imePadding().fillMaxSize().verticalScroll(rememberScrollState()).padding(28.dp),verticalArrangement=Arrangement.Center) {
        Text("N",color=MaterialTheme.colorScheme.primary,style=MaterialTheme.typography.displayLarge)
        Spacer(Modifier.height(20.dp)); Text("连接你的 NEXUS",style=MaterialTheme.typography.headlineMedium,fontWeight=FontWeight.Bold)
        Text("一个中心，多个设备。",color=MaterialTheme.colorScheme.secondary,modifier=Modifier.padding(vertical=12.dp))
        OutlinedTextField(url,{url=it},label={Text("服务地址（HTTPS）")},singleLine=true,modifier=Modifier.fillMaxWidth())
        OutlinedTextField(token,{token=it},label={Text("登录凭证")},visualTransformation=PasswordVisualTransformation(),singleLine=true,modifier=Modifier.fillMaxWidth().padding(top=12.dp))
        Text("登录令牌只用于创建会话。手机保存加密会话，不保存模型密钥。",style=MaterialTheme.typography.bodySmall,modifier=Modifier.padding(vertical=16.dp))
        Button(onClick={val secret=token; token=""; model.login(url,secret)},enabled=!model.busy && url.isNotBlank() && token.isNotBlank(),modifier=Modifier.fillMaxWidth()) { Text(if(model.busy) "正在连接…" else "连接") }
        Notice(model)
    } }
}
@Composable private fun Notice(model: AppModel) {
    if(model.notice.isNotEmpty()) Card(Modifier.fillMaxWidth().padding(horizontal=16.dp,vertical=4.dp)) {
        Text(model.notice,Modifier.padding(12.dp),color=MaterialTheme.colorScheme.error)
    }
}
@Composable private fun PendingPanel(model: AppModel) {
    var confirm by remember { mutableStateOf(false) }
    val pending = model.pending ?: return
    Card(Modifier.fillMaxWidth().padding(12.dp)) { Column(Modifier.padding(12.dp)) {
        Text("上次操作需要核对",fontWeight=FontWeight.Bold)
        Text("上次操作可能已经提交，正在同步最新结果。请先核对，避免重复操作。",style=MaterialTheme.typography.bodySmall)
        Row {
            if(pending.text("retryable")=="true" && pending.text("url")==model.client.base.toString()) TextButton(onClick={model.retryPending()},enabled=!model.busy) { Text("核对上次操作") }
            TextButton(onClick={confirm=true},enabled=!model.busy) { Text("已人工核对") }
        }
    } }
    if(confirm) AlertDialog(onDismissRequest={confirm=false},title={Text("清除本地核对标记？")},text={Text("请先查看会话或任务记录。此操作不会取消服务端任务；再次发送新内容会使用新编号。")},
        confirmButton={TextButton(onClick={model.acknowledgePending();confirm=false}){Text("已核对，清除")}},dismissButton={TextButton(onClick={confirm=false}){Text("返回")}})
}
@Composable private fun Toggle(label: String, value: Boolean, enabled: Boolean = true, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(),verticalAlignment=Alignment.CenterVertically) {
        Checkbox(value,onChange,enabled=enabled); Text(label,style=MaterialTheme.typography.bodySmall,modifier=Modifier.weight(1f))
    }
}
@Composable private fun ChatPage(model: AppModel) {
    var showScene by rememberSaveable { mutableStateOf(false) }
    var menu by remember { mutableStateOf(false) }
    var listOpen by remember { mutableStateOf(false) }
    var rename by remember { mutableStateOf(false) }
    var title by remember { mutableStateOf("") }
    var options by remember { mutableStateOf(false) }
    var sendConsent by remember { mutableStateOf(false) }
    var archiveConfirm by remember { mutableStateOf(false) }
    val messageList = rememberLazyListState()
    val followLatest by remember { derivedStateOf { !messageList.canScrollForward } }
    val turns = (model.olderTurns + model.history.objects("turns")).distinctBy { it.text("turn_id") }
    LaunchedEffect(model.conversationId,turns.lastOrNull()) {
        if(followLatest && turns.isNotEmpty()) messageList.animateScrollToItem(turns.size)
    }
    BoxWithConstraints(Modifier.fillMaxSize()) {
    val expanded = maxWidth >= 840.dp
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.padding(horizontal=12.dp),verticalAlignment=Alignment.CenterVertically) {
            TextButton(onClick={listOpen=true}) { Text("会话") }
            Text(model.history.objectAt("conversation").text("title").ifEmpty { "开始一段对话" },Modifier.weight(1f),maxLines=1,overflow=TextOverflow.Ellipsis)
            TextButton(onClick={model.newConversation()},enabled=!model.busy && model.pending==null) { Text("新建") }
            if(!expanded) TextButton(onClick={showScene=!showScene}) { Text(if(showScene) "对话" else "动态") }
            Box {
                TextButton(onClick={menu=true}) { Text("更多") }
                DropdownMenu(expanded=menu,onDismissRequest={menu=false}) {
                    DropdownMenuItem(text={Text("重命名")},onClick={title=model.history.objectAt("conversation").text("title");rename=true;menu=false},enabled=model.conversationId.isNotEmpty())
                    DropdownMenuItem(text={Text(if(model.history.objectAt("conversation").text("archived")=="1") "恢复会话" else "归档会话")},onClick={menu=false;if(model.history.objectAt("conversation").text("archived")=="1")model.updateConversation(archive=false) else archiveConfirm=true},enabled=model.conversationId.isNotEmpty())
                }
            }
        }
        if(model.voiceActive) Card(Modifier.fillMaxWidth().padding(12.dp)) { Column(Modifier.padding(12.dp)) {
            val label=mapOf("activating" to "正在连接","listening" to "正在听你说","thinking" to "正在思考","speaking" to "正在回答","interrupted" to "已打断","stopping" to "正在结束")
            Text(label[model.voiceState] ?: "语音会话",color=MaterialTheme.colorScheme.primary)
            Text(model.voiceDetail,style=MaterialTheme.typography.bodySmall)
            Row { TextButton(onClick={model.interruptVoice()}){Text("打断回答")};TextButton(onClick={model.stopVoice()}){Text("结束语音")}}
        } }
        Row(Modifier.weight(1f).fillMaxWidth()) {
            if(expanded || showScene || turns.isEmpty()) Column(Modifier.then(if(expanded) Modifier.weight(.42f) else Modifier.weight(1f)).fillMaxHeight().padding(12.dp)) {
                Surface(Modifier.weight(1f).fillMaxWidth(),shape=RoundedCornerShape(22.dp),color=Color(0xFF080A0E)) { Visualization(model,Modifier.fillMaxSize()) }
                if(turns.isEmpty()) { Text("想一起完成什么？",Modifier.padding(top=16.dp),style=MaterialTheme.typography.titleLarge);Text("直接输入，也可以用语音聊聊。",Modifier.padding(top=6.dp,bottom=12.dp),color=MaterialTheme.colorScheme.secondary) }
                else Text(if(model.activeTurn.isNotEmpty()) "正在处理你的请求" else "设备动态 · 与对话同步",Modifier.padding(8.dp),style=MaterialTheme.typography.bodySmall,color=MaterialTheme.colorScheme.secondary)
            }
            if((expanded || !showScene) && turns.isNotEmpty()) LazyColumn(Modifier.weight(if(expanded) .58f else 1f).fillMaxHeight(),state=messageList,contentPadding=PaddingValues(16.dp),verticalArrangement=Arrangement.spacedBy(18.dp)) {
                item { TextButton(onClick={model.earlierTurns()},enabled=!model.busy){Text("更早消息")} }
                items(turns,key={it.text("turn_id")}) { turn -> TurnCard(model,turn) }
            }
        }
        if(model.activeTurn.isNotEmpty()) TextButton(onClick={model.stopTurn(model.activeTurn)},enabled=!model.busy && model.pending==null,modifier=Modifier.align(Alignment.End)) { Text("停止当前回复") }
        Column(Modifier.background(MaterialTheme.colorScheme.surface).padding(horizontal=16.dp,vertical=8.dp)) {

            if(options) Column(Modifier.heightIn(max=240.dp).verticalScroll(rememberScrollState())) {
                Toggle("允许将本轮消息发给模型",model.allowModel){model.allowModel=it}
                Toggle("携带本会话上下文",model.allowContext){model.allowContext=it}
                Toggle("允许助手使用已授权设备",model.allowTools){model.allowTools=it}
                Toggle("允许模型读取本轮执行结果（可能含通知）",model.allowResults){model.allowResults=it}

            }
            if(model.sendBlockedReason.isNotEmpty()) Text(model.sendBlockedReason,style=MaterialTheme.typography.labelSmall,color=MaterialTheme.colorScheme.secondary)
            OutlinedTextField(model.draft,model::editDraft,label={Text("输入消息")},maxLines=5,modifier=Modifier.fillMaxWidth(),enabled=!model.busy)
            Row(Modifier.fillMaxWidth(),verticalAlignment=Alignment.CenterVertically) {
                TextButton(onClick={model.showVoice()},enabled=!model.busy && !model.voiceActive && model.pending==null) { Text("语音") }
                TextButton(onClick={options=!options}) { Text(if(options) "收起选项" else "选项") }
                Spacer(Modifier.weight(1f))
                Button(onClick={
                    if(!model.allowModel || model.allowTools && !model.allowResults) sendConsent=true else model.send()
                },enabled=model.sendBlockedReason.isEmpty() && model.draft.isNotBlank()) { Text("发送") }
            }
        }
    }
    }
    if(sendConsent) AlertDialog(onDismissRequest={sendConsent=false},title={Text("发送本轮消息")},text={Column {
        Text("本轮文字${if(model.allowContext) "和会话上下文" else ""}将发送给模型。")
        if(model.allowTools) Text("助手可使用已授权设备，相关执行结果会交给模型，可能包含文件、图片或通知内容。需要确认的操作仍会询问你。",Modifier.padding(top=12.dp))
    }},confirmButton={TextButton(onClick={
        sendConsent=false;model.allowModel=true;model.allowResults=model.allowTools;model.send()
    },enabled=model.sendBlockedReason.isEmpty()){Text("同意并发送")}},dismissButton={TextButton(onClick={sendConsent=false}){Text("返回")}})
    if(listOpen) AlertDialog(onDismissRequest={listOpen=false},title={Text("会话记录")},text={Column {
        Toggle("查看归档",model.archived){model.listConversations(it)}
        LazyColumn(Modifier.heightIn(max=400.dp)) {
            items(model.conversations,key={it.text("conversation_id")}) { value -> TextButton(onClick={model.selectConversation(value.text("conversation_id"));listOpen=false}){Text(value.text("title"))} }
            item { TextButton(onClick={model.moreConversations()},enabled=!model.busy){Text("加载更多")}}
        }
    }},confirmButton={TextButton(onClick={listOpen=false}){Text("关闭")}})
    if(rename) AlertDialog(onDismissRequest={rename=false},title={Text("重命名")},text={OutlinedTextField(title,{title=it.take(80)})},confirmButton={TextButton(onClick={model.updateConversation(title=title);rename=false},enabled=title.isNotBlank()){Text("保存")}})
    if(archiveConfirm) AlertDialog(onDismissRequest={archiveConfirm=false},title={Text("归档会话？")},text={Text("归档会停止此会话的工作授权，可在归档记录中恢复。")},confirmButton={TextButton(onClick={model.updateConversation(archive=true);archiveConfirm=false}){Text("归档")}},dismissButton={TextButton(onClick={archiveConfirm=false}){Text("返回")}})
}
@Composable private fun TurnCard(model: AppModel, turn: JsonObject) {
    val presentation=turn.objectAt("presentation")
    val steps=turn.objects("work_steps")
    val processes=presentation.objects("process")
    val attention=steps.any{it.text("status") in setOf("permission","failed","unknown")}
    var details by rememberSaveable(turn.text("turn_id")) { mutableStateOf(false) }
    val live=turn.text("turn_id")==model.activeTurn
    Column(Modifier.fillMaxWidth(),verticalArrangement=Arrangement.spacedBy(14.dp)) {
        Surface(Modifier.align(Alignment.End).fillMaxWidth(.88f),color=Color(0xFF181818),shape=RoundedCornerShape(18.dp,18.dp,5.dp,18.dp),border=androidx.compose.foundation.BorderStroke(1.dp,Color(0xFF333333))) {
            SelectionContainer { Text(turn.text("text").ifEmpty{turn.text("input_text")},Modifier.padding(14.dp)) }
        }
        Column(Modifier.fillMaxWidth().padding(horizontal=2.dp),verticalArrangement=Arrangement.spacedBy(8.dp)) {
            Text("NEXUS",fontWeight=FontWeight.SemiBold,style=MaterialTheme.typography.labelMedium)
            if(live || attention || turn.text("status") !in setOf("completed","")) Text(presentation.text("label").ifEmpty{statusLabel(turn.text("status"))},style=MaterialTheme.typography.labelMedium,color=MaterialTheme.colorScheme.secondary)
            val reply=presentation.text("final_text").ifEmpty{presentation.text("live_text").ifEmpty{turn.text("response_text")}}
            if(reply.isNotEmpty()) Markdown(reply)
            ChatFileCards(model,presentation.objects("files"))
            if(steps.isNotEmpty() || processes.isNotEmpty() || turn.objectAt("error").isNotEmpty()) {
                TextButton(onClick={details=!details}){Text(if(details) "收起过程" else "查看处理过程${if(steps.isNotEmpty()) " · ${steps.size} 步" else ""}")}
                if(details || live || attention) {
                    processes.takeLast(8).forEach { Text(it.text("text"),style=MaterialTheme.typography.bodySmall) }
                    for(step in steps) {
                        Text(listOf(step.text("node_label"),actionLabel(step.objectAt("proposal").text("intent")),statusLabel(step.text("status"))).filter{it.isNotBlank()}.joinToString(" · "),style=MaterialTheme.typography.bodySmall)
                        if(step.text("task_id").isNotEmpty()) TextButton(onClick={model.page=1;model.viewTask(step.text("task_id"))},enabled=!model.busy){Text(if(step.text("status")=="permission") "查看并确认" else "查看任务")}
                        if(step.text("status")=="permission") {
                            Text(step.objectAt("proposal").text("reason").ifEmpty{"此操作需要你确认，请查看详细内容。"},style=MaterialTheme.typography.bodySmall)
                            TechnicalDetails(step)
                            Row { TextButton(onClick={model.answer(turn,step,"同意")},enabled=!model.busy && model.pending==null){Text("同意本步骤")};TextButton(onClick={model.answer(turn,step,"拒绝")},enabled=!model.busy && model.pending==null){Text("拒绝")}}
                        }
                    }
                    if(details) TechnicalDetails(buildJsonObject { put("work_steps",JsonArray(steps));put("error",turn["error"] ?: JsonNull) })
                }
            }
        }
    }
}
@Composable private fun TechnicalDetails(value:JsonElement) {
    var open by remember { mutableStateOf(false) }
    TextButton(onClick={open=!open}) { Text(if(open) "收起技术详情" else "技术详情") }
    if(open) SelectionContainer { Text(formatted(value),style=MaterialTheme.typography.bodySmall) }
}
@Composable private fun TasksPage(model: AppModel) {
    var approval by remember { mutableStateOf<Pair<String,String>?>(null) }
    val task = model.taskDetail.objectAt("task")
    LazyColumn(Modifier.fillMaxSize(),contentPadding=PaddingValues(16.dp),verticalArrangement=Arrangement.spacedBy(12.dp)) {
        item { Text("任务与审批",style=MaterialTheme.typography.headlineSmall) }
        if(task.isNotEmpty()) item { Card { Column(Modifier.padding(16.dp)) {
            Text(actionLabel(task.objectAt("action").text("tool").ifEmpty{task.text("intent")}),fontWeight=FontWeight.Bold)
            Text(statusLabel(task.text("status")),color=MaterialTheme.colorScheme.secondary)
            task.objectAt("action").let { action ->
                if(action.isNotEmpty()) Text("操作内容：${formatted(action)}",style=MaterialTheme.typography.bodySmall)
            }
            TechnicalDetails(model.taskDetail)
            model.taskDetail.objects("approvals").filter { it.text("decision").isEmpty() }.forEach { value ->
                Text("审批期限：${value.text("expires_at")}",style=MaterialTheme.typography.bodySmall)
                Row { TextButton(onClick={approval=value.text("approval_id") to "approve"},enabled=!model.busy && model.pending==null){Text("批准")};TextButton(onClick={model.decide(value.text("approval_id"),"reject")},enabled=!model.busy && model.pending==null){Text("拒绝")}}
            }
            if(task.text("status") !in setOf("succeeded","failed","cancelled","expired","rejected","timed_out")) TextButton(onClick={model.cancelTask()},enabled=!model.busy && model.pending==null){Text("取消任务")}
            TextButton(onClick={model.taskDetail=JsonObject(emptyMap())}){Text("关闭详情")}
        } } }
        if(model.tasks.isEmpty()) item { Text("暂无任务。新的任务和审批会自动显示。") }
        items(model.tasks.reversed(),key={it.text("task_id")}) { value -> OutlinedCard(onClick={model.viewTask(value.text("task_id"))},modifier=Modifier.fillMaxWidth()) { Column(Modifier.padding(16.dp)) {
            Text(actionLabel(value.objectAt("action").text("tool").ifEmpty{value.text("intent")}))
            Text(statusLabel(value.text("status")),color=MaterialTheme.colorScheme.secondary,style=MaterialTheme.typography.bodySmall)
        } } }
        if(model.tasksMore) item { TextButton(onClick={model.taskPage(true)},enabled=!model.busy){Text("继续加载任务")}}
    }
    approval?.let { (id,decision) -> AlertDialog(onDismissRequest={approval=null},title={Text("批准此具体执行？")},text={Text("请核对上方任务的工具、参数、设备和有效期。同意仅用于本次操作，通知文字可能返回助手。")},confirmButton={TextButton(onClick={model.decide(id,decision);approval=null}){Text("批准")}},dismissButton={TextButton(onClick={approval=null}){Text("返回")}}) }
}
@Composable private fun DevicesPage(model: AppModel, state: String, running: Boolean, start: () -> Unit, stop: () -> Unit, settings: () -> Unit) {
    val context = LocalContext.current
    val vault = remember { Vault(context,"node") }
    val saved = remember { runCatching{vault.read()}.getOrNull() }
    var url by remember { mutableStateOf(saved?.text("url") ?: model.client.base.toString()) }
    var id by remember { mutableStateOf(saved?.text("node_id") ?: "") }
    var token by remember { mutableStateOf("") }
    var packages by remember { mutableStateOf((saved?.get("packages") as? JsonArray)?.joinToString("\n"){it.jsonPrimitive.content} ?: "") }
    var configured by remember { mutableStateOf(saved != null) }
    var receiveFiles by remember { mutableStateOf(saved?.get("receive_files")?.jsonPrimitive?.boolean ?: true) }
    var connectionOpen by remember { mutableStateOf(saved == null) }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),verticalArrangement=Arrangement.spacedBy(12.dp)) {
        Text("我的设备",style=MaterialTheme.typography.headlineSmall)
        model.nodes.forEach { node -> DeviceCard(model,node) }
        HorizontalDivider()
        Text("让助手使用这台设备",style=MaterialTheme.typography.titleMedium)
        Text(state,color=MaterialTheme.colorScheme.primary)
        Text("开启后，助手可以在你授权的范围内使用本机功能。",style=MaterialTheme.typography.bodySmall)
        Row { Button(onClick=start,enabled=configured && !running){Text("开启")};Spacer(Modifier.width(12.dp));OutlinedButton(onClick=stop,enabled=running){Text("停止")}}
        TextButton(onClick={connectionOpen=!connectionOpen}){Text(if(connectionOpen) "收起连接设置" else "连接与权限")}
        if(connectionOpen) Column(verticalArrangement=Arrangement.spacedBy(12.dp)) {
        OutlinedTextField(url,{url=it},label={Text("服务地址")},enabled=!running,singleLine=true,modifier=Modifier.fillMaxWidth())
        OutlinedTextField(id,{id=it},label={Text("设备编号")},enabled=!running,singleLine=true,modifier=Modifier.fillMaxWidth())
        OutlinedTextField(token,{token=it},label={Text(if(configured) "新设备凭证（留空保留）" else "设备凭证")},enabled=!running,visualTransformation=PasswordVisualTransformation(),singleLine=true,modifier=Modifier.fillMaxWidth())
        Row { Checkbox(checked=receiveFiles,onCheckedChange={receiveFiles=it},enabled=!running);Text("允许接收文件到本机收件箱") }
        Text("通知读取默认关闭。只有下列应用在授权开启后新到达的通知可被读取；最多缓存 100 条，并遮蔽验证码。",style=MaterialTheme.typography.bodySmall)
        OutlinedTextField(packages,{packages=it.take(6400)},label={Text("允许的应用包名，每行一个；留空关闭")},enabled=!running,maxLines=5,modifier=Modifier.fillMaxWidth())
        Row {
            Button(onClick={runCatching {
                val old = vault.read()
                require(token.isNotBlank() || old?.text("node_id")==id.trim() && old.text("url")==url.trim()) { "更换设备身份或地址时请提供设备凭证。" }
                val config = NodeConfig(url.trim(),id.trim(),token.trim().ifEmpty{old?.text("token") ?: ""},packages.split(Regex("[\\s,]+" )).filter{it.isNotEmpty()}.toSet(),receiveFiles)
                vault.write(obj("url" to str(config.url),"node_id" to str(config.nodeId),"token" to str(config.token),"packages" to JsonArray(config.packages.sorted().map(::str)),"receive_files" to JsonPrimitive(config.receiveFiles)))
                token="";configured=true;model.notice="已保存连接设置。设备需要服务端授权后才能使用。"
            }.onFailure { model.notice=it.message ?: "设备配置无法保存" }},enabled=!running){Text("保存配置")}
            TextButton(onClick=settings){Text("系统通知读取权限")}
        }
        }
    }
}
@Composable private fun DeviceCard(model: AppModel, node: JsonObject) {
    var editing by remember { mutableStateOf(false) }
    var name by remember { mutableStateOf("") }
    Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(12.dp)) {
            Text(node.text("label").ifEmpty{"设备"})
            TextButton(onClick={name=node.text("label");editing=true}){Text("修改名称")}
            Text(if(node.text("online")=="true") "在线" else "离线",color=MaterialTheme.colorScheme.secondary)
            if(node.toString().contains(DEVICE)) TextButton(onClick={model.deviceStatus(node.text("node_id"))},enabled=!model.busy && model.pending==null){Text("读取设备状态")}
        } }
    if(editing) AlertDialog(onDismissRequest={editing=false},title={Text("设备名称")},
        text={Column { Text("例如：书房电脑、工作手机。聊天时直接使用这个名称。")
            OutlinedTextField(name,{name=it.take(40)},singleLine=true,label={Text("名称")}) }},
        confirmButton={TextButton(onClick={model.renameNode(node.text("node_id"),name);editing=false},enabled=name.isNotBlank()){Text("保存")}},
        dismissButton={TextButton(onClick={editing=false}){Text("取消")}})
}
@Composable private fun SettingsPage(model: AppModel, stop: () -> Unit) {
    var logout by remember { mutableStateOf(false) }
    Column(Modifier.padding(24.dp).verticalScroll(rememberScrollState()),verticalArrangement=Arrangement.spacedBy(16.dp)) {
        Text("连接与隐私",style=MaterialTheme.typography.headlineSmall)
        Text("聊天记录在你的服务中保存；登录与设备凭证在本机加密保存。",color=MaterialTheme.colorScheme.secondary)
        Text("语音只在前台开启。切到后台或锁屏时会关麦，恢复页面不会自动开麦。",style=MaterialTheme.typography.bodySmall)
        var advanced by remember { mutableStateOf(false) }
        TextButton(onClick={advanced=!advanced}){Text(if(advanced) "收起连接详情" else "连接详情")}
        if(advanced) { Text(model.client.base.toString());Text("设备身份与执行记录绑定，更换前需核对未完成操作。",style=MaterialTheme.typography.bodySmall) }
        ReceivedFiles()
        model.fileUpload?.let { saved ->
            Text("旧版上传待核对：${saved.text("filename")}")
            TextButton(onClick=model::retryFileUpload,enabled=!model.busy){Text("核对原上传")}
            TextButton(onClick=model::dismissFileUpload,enabled=!model.busy){Text("保留服务器文件并清除本机标记")}
        }
        OutlinedButton(onClick={logout=true},enabled=!model.busy){Text("退出登录并停止设备")}
        Text("Android ${BuildConfig.VERSION_NAME}",style=MaterialTheme.typography.labelSmall)
    }
    if(logout) AlertDialog(onDismissRequest={logout=false},title={Text("退出登录？")},text={Text("设备执行将停止，本地登录会话和草稿将清除。已提交的任务不会因此取消；未确认操作与设备执行记录保留。")},confirmButton={TextButton(onClick={stop();model.logout();logout=false}){Text("退出")}},dismissButton={TextButton(onClick={logout=false}){Text("返回")}})
}
@Composable private fun VoiceDialog(model: AppModel, start: () -> Unit) {
    AlertDialog(onDismissRequest={model.voiceDialog=false},title={Text("开始语音")},text={Column {
        Text(model.voiceInfo.text("audio_disclosure"))
        Text("仅本次会话和本机设备有效。最多 ${model.voiceInfo.text("max_session_seconds")} 秒；切到后台即关麦。首次使用请在安静环境配合说话。",style=MaterialTheme.typography.bodySmall,modifier=Modifier.padding(vertical=12.dp))
        Toggle("携带当前会话上下文",model.allowContext){model.allowContext=it}
        Toggle("允许与文字聊天相同的已授权工具，并外发工具结果",model.voiceTools){model.voiceTools=it}
        Text("回答播放时暂停收音，点击“打断回答”后可以继续说话。",style=MaterialTheme.typography.bodySmall)
    }},confirmButton={TextButton(onClick={model.voiceDialog=false;start()}){Text("同意外发并开麦")}},dismissButton={TextButton(onClick={model.voiceDialog=false}){Text("取消")}})
}
