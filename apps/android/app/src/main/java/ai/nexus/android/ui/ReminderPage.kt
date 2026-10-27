package ai.nexus.android.ui

import android.app.NotificationManager
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import ai.nexus.android.client.*
import ai.nexus.android.reminders.ReminderService
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*

@Composable fun ReminderPage(model:AppModel,start:()->Unit) {
    val context=LocalContext.current
    val scope=rememberCoroutineScope()
    var inbox by remember { mutableStateOf(emptyList<JsonObject>()) }
    var schedules by remember { mutableStateOf(emptyList<JsonObject>()) }
    var goals by remember { mutableStateOf(emptyList<JsonObject>()) }
    var clients by remember { mutableStateOf(emptyList<JsonObject>()) }
    var message by remember { mutableStateOf("") }
    var working by remember { mutableStateOf(false) }
    var name by remember { mutableStateOf(android.os.Build.MODEL) }
    var enabled by remember { mutableStateOf(context.getSharedPreferences("reminder-status",0).getBoolean("enabled",false)) }
    fun explain(error:Exception) = if(error is ApiFailure) error.message ?: "服务暂时不可用" else "暂时无法连接中心服务，请检查网络；恢复后会自动同步提醒。"
    suspend fun refresh() {
        inbox=model.client.request("GET","/api/v1/reminders").objects("reminders")
        val automation=model.client.request("GET","/api/v1/plugins/automation")
        schedules=automation.objects("schedules");goals=automation.objects("goals")
        clients=model.client.request("GET","/api/v1/reminders/clients").objects("clients")
        enabled=context.getSharedPreferences("reminder-status",0).getBoolean("enabled",false)
        message=context.getSharedPreferences("reminder-status",0).getString("state","") ?: ""
    }
    fun action(block:suspend ()->Unit) { scope.launch {
        working=true
        try { block();refresh() } catch(e:CancellationException) { throw e } catch(e:Exception) { message=explain(e) } finally { working=false }
    } }
    LaunchedEffect(model.appForeground) {
        while(model.appForeground) {
            try { refresh() } catch(e:CancellationException) { throw e } catch(e:Exception) { message=explain(e) }
            delay(5000)
        }
    }
    LazyColumn(Modifier.fillMaxSize().padding(horizontal=20.dp),verticalArrangement=Arrangement.spacedBy(12.dp)) {
        item {
            Text("提醒与持续任务",style=MaterialTheme.typography.headlineSmall)
            Text("在对话中说明时间、工作内容和接收设备。周期任务默认持续到手动停止。")
            OutlinedTextField(name,{name=it.take(80)},label={Text("这台手机的提醒名称")},singleLine=true,modifier=Modifier.fillMaxWidth())
            Button(enabled=!working && name.isNotBlank(),onClick={action {
                val vault=Vault(context,"reminders")
                val id=vault.read()?.text("client_id")?.takeIf{it.isNotEmpty()} ?: uid()
                val value=model.client.request("POST","/api/v1/reminders/clients",obj("client_id" to str(id),"name" to str(name),"platform" to str("android")))
                vault.write(JsonObject(value+mapOf("url" to str(model.client.base.toString()))))
                start()
            }}) { Text(if(enabled) "重新连接后台提醒" else "开启后台提醒") }
            if(enabled) TextButton(onClick={action {
                context.getSharedPreferences("reminder-status",0).edit().putBoolean("enabled",false).apply()
                context.stopService(Intent(context,ReminderService::class.java))
                Vault(context,"reminders").read()?.text("client_id")?.let { model.client.request("DELETE","/api/v1/reminders/clients/$it") }
            }}) { Text("停止这台手机接收") }
            Text("后台提醒不依赖 Google 服务，保留常驻通知并维持后台连接，会增加耗电。红魔请允许后台运行／自启动，并关闭本 App 的省电限制。",style=MaterialTheme.typography.bodySmall)
            TextButton(onClick={
                val power=context.getSystemService(PowerManager::class.java)
                val intent=if(power.isIgnoringBatteryOptimizations(context.packageName)) Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:${context.packageName}"))
                    else Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,Uri.parse("package:${context.packageName}"))
                runCatching{context.startActivity(intent)}.onFailure{message="请在系统应用设置中允许后台运行"}
            }) { Text("设置锁屏后台运行") }
            TextButton(onClick={context.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE,context.packageName))}) { Text("系统通知与锁屏显示") }
            if(message.isNotBlank()) Text(message,style=MaterialTheme.typography.bodySmall)
            TextButton(onClick={model.editDraft("每天早上9点提醒我查看今天的安排，持续到我停止，发到所有已启用提醒的设备");model.page=0}) { Text("在对话中创建任务") }
        }
        item { Text("接收设备",style=MaterialTheme.typography.titleLarge) }
        for(row in clients) item(key=row.text("client_id")) {
            Text("${row.text("name")} · ${if(row.text("enabled")=="1") "已启用" else "已关闭"} · ${row.text("platform")}")
        }
        item { Text("持续任务",style=MaterialTheme.typography.titleLarge) }
        for(row in schedules+goals) item(key=row.text("schedule_id").ifEmpty{row.text("goal_id")}) {
            val schedule=row.text("schedule_id").isNotEmpty()
            val id=if(schedule) row.text("schedule_id") else row.text("goal_id")
            val paused=if(schedule) row.text("paused")=="1" else row.text("status")!="active"
            val continuous=row.text("run_until_stopped") in setOf("1","true")
            val ended=schedule && !continuous && ((row.text("runs").toIntOrNull() ?: 0)>=(row.text("max_runs").toIntOrNull() ?: Int.MAX_VALUE) || runCatching{java.time.Instant.parse(row.text("expires_at")).isBefore(java.time.Instant.now())}.getOrDefault(false))
            Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(14.dp)) {
                Text(row.text("title").ifEmpty{row.text("objective")},style=MaterialTheme.typography.titleMedium)
                Text(if(row.text("status")=="complete") "已完成" else if(ended) "已结束" else if(continuous) "无结束日期 · ${if(paused) "已暂停" else "进行中"}" else "${if(paused) "已暂停" else "进行中"} · 到期 ${row.text("expires_at")}")
                if(schedule && !ended) Text("下次 ${row.text("next_at")}")
                Row {
                    if(!ended && row.text("status")!="complete") TextButton(enabled=!working,onClick={action {
                        val changes=if(schedule) obj("paused" to JsonPrimitive(!paused)) else obj("status" to str(if(paused)"active" else "paused"))
                        model.client.request("PATCH","/api/v1/plugins/conversations/${row.text("conversation_id")}/${if(schedule)"schedules" else "goals"}/$id",obj("request_id" to str(uid()),"changes" to changes))
                    }}) { Text(if(paused)"恢复" else "暂停") }
                    if(schedule) TextButton(enabled=!working,onClick={action {
                        model.client.request("DELETE","/api/v1/plugins/conversations/${row.text("conversation_id")}/schedules/$id",obj("request_id" to str(uid())))
                    }}) { Text("停止后续运行") }
                    TextButton(onClick={model.selectConversation(row.text("conversation_id"));model.page=0}) { Text("来源会话") }
                }
            } }
        }
        item { Text("提醒内容",style=MaterialTheme.typography.titleLarge) }
        if(inbox.isEmpty()) item { Text("暂无提醒") }
        for(row in inbox) item(key=row.text("reminder_id")) {
            Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(14.dp)) {
                Text(row.text("title"),style=MaterialTheme.typography.titleMedium);Text(row.text("body"))
                Text(row.text("created_at"),style=MaterialTheme.typography.bodySmall)
                Row {
                    TextButton(onClick={action {
                        model.client.request("POST","/api/v1/reminders/${row.text("reminder_id")}/read")
                        context.getSystemService(NotificationManager::class.java).cancel(row.text("reminder_id"),1)
                    }}) { Text(if(row.text("read_at").isEmpty()) "标记已读" else "已读") }
                    TextButton(onClick={model.selectConversation(row.text("conversation_id"));model.page=0}) { Text("打开结果") }
                }
            } }
        }
    }
}
