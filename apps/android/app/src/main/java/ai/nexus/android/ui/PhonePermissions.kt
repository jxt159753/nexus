package ai.nexus.android.ui

import android.accessibilityservice.AccessibilityServiceInfo
import android.content.Intent
import android.provider.DocumentsContract
import android.provider.Settings
import android.view.accessibility.AccessibilityManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import ai.nexus.android.client.AppModel
import ai.nexus.android.client.objectAt
import ai.nexus.android.client.text
import ai.nexus.android.node.PhoneAccess
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject

@Composable fun PhoneWritePreview(step: JsonObject) {
    Text(step.text("request_text"))
    val preview=step.objectAt("result").objectAt("preview")
    Text(preview.text("diff").ifEmpty { "文件内容没有变化。" },
        fontFamily=androidx.compose.ui.text.font.FontFamily.Monospace,
        style=MaterialTheme.typography.bodySmall,
        modifier=Modifier.fillMaxWidth().heightIn(max=240.dp).verticalScroll(rememberScrollState()))
    if(preview.text("truncated")=="true") Text("差异较长，当前仅显示前一部分。",style=MaterialTheme.typography.bodySmall)
}

@Composable fun PhonePermissions(model: AppModel) {
    val context=LocalContext.current
    val access=remember { PhoneAccess(context) }
    var revision by remember { mutableIntStateOf(0) }
    var accessibility by remember { mutableStateOf(false) }
    var vision by remember { mutableStateOf(access.enabled("vision")) }
    var files by remember { mutableStateOf(false) }
    var enabled by remember { mutableStateOf(false) }
    var roots by remember { mutableStateOf(emptyList<Pair<android.net.Uri,String>>()) }
    val picker=rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val data=result.data;val uri=data?.data
        if(result.resultCode==android.app.Activity.RESULT_OK && uri!=null) runCatching {
            require(DocumentsContract.isTreeUri(uri))
            val flags=data.flags and (Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            require(flags and Intent.FLAG_GRANT_READ_URI_PERMISSION!=0) { "所选目录没有读取授权。" }
            if(flags and Intent.FLAG_GRANT_WRITE_URI_PERMISSION!=0)
                context.contentResolver.takePersistableUriPermission(uri,Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            else context.contentResolver.takePersistableUriPermission(uri,Intent.FLAG_GRANT_READ_URI_PERMISSION)
            revision++
        }.onFailure { model.notice=it.message ?: "目录授权未完成。" }
    }
    val settings=rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { revision++ }
    LaunchedEffect(revision) {
        runCatching {
            withContext(Dispatchers.IO) {
                val allowed=context.getSystemService(AccessibilityManager::class.java).getEnabledAccessibilityServiceList(AccessibilityServiceInfo.FEEDBACK_ALL_MASK)
                    .any { it.resolveInfo.serviceInfo.packageName==context.packageName && it.resolveInfo.serviceInfo.name.endsWith(".PhoneAccessibility") }
                val selected=context.contentResolver.persistedUriPermissions.filter { it.isReadPermission && DocumentsContract.isTreeUri(it.uri) }.map { grant ->
                    val doc=DocumentsContract.buildDocumentUriUsingTree(grant.uri,DocumentsContract.getTreeDocumentId(grant.uri))
                    val label=runCatching { context.contentResolver.query(doc,arrayOf(DocumentsContract.Document.COLUMN_DISPLAY_NAME),null,null,null)?.use { if(it.moveToFirst()) it.getString(0) else null } }.getOrNull() ?: "授权已失效的目录"
                    grant.uri to (label + if(grant.isWritePermission) " · 可读写" else " · 只读")
                }
                Pair(Triple(access.enabled("accessibility"),access.enabled("files"),allowed),selected)
            }
        }.onSuccess { (states,selected) -> accessibility=states.first;files=states.second;enabled=states.third;roots=selected }
            .onFailure { model.notice="无法读取本机权限设置，请核对系统授权。" }
    }
    if(model.deviceSyncNotice.isNotBlank()) Text(model.deviceSyncNotice,style=MaterialTheme.typography.bodySmall)
    Disclosure("手机操作与文件",listOf(if(accessibility) "无障碍已允许" else "无障碍未开启",if(vision) "视觉已允许" else "视觉未开启",if(files) "文件已允许" else "文件未开启").joinToString(" · ")) {
        Text("本机权限开关会自动同步到 Core。系统授权仍需你手动开启；关闭本机开关立即停止后续操作。",style=MaterialTheme.typography.bodySmall)
        Row { Switch(accessibility,onCheckedChange={ value -> runCatching { access.set("accessibility",value);accessibility=value;revision++;model.syncDevicePermissions() }.onFailure { model.notice="设置未保存。" } });Text("允许无障碍操作",Modifier.padding(12.dp)) }
        Text(if(enabled) "无障碍服务：已在系统启用" else "无障碍服务：尚未启用",style=MaterialTheme.typography.bodySmall)
        Text("可读取当前界面、点击、输入、滚动和返回；密码字段不读取。系统授权界面需要你亲自操作。",style=MaterialTheme.typography.bodySmall)
        TextButton(onClick={runCatching { settings.launch(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }.onFailure { model.notice="请在系统设置中手动打开无障碍服务。" }}) { Text("打开系统无障碍设置") }
        Row { Switch(vision,enabled=android.os.Build.VERSION.SDK_INT>=30,onCheckedChange={value -> runCatching { access.set("vision",value);vision=value;revision++;model.syncDevicePermissions() }.onFailure { model.notice="设置未保存。" } });Text("允许截图与视觉操作",Modifier.padding(12.dp)) }
        Text("仅在获准任务操作时截图，画面会发送至现有 DeepSeek 识别，可能包含当前聊天内容；支持坐标点击、长按和滑动。需要 Android 11 及以上，本机无障碍也须开启。关闭后停止新的截图与视觉操作。",style=MaterialTheme.typography.bodySmall)
        HorizontalDivider()
        Row { Switch(files,onCheckedChange={value -> runCatching { access.set("files",value);files=value;revision++;model.syncDevicePermissions() }.onFailure { model.notice="设置未保存。" } });Text("允许读写所选目录",Modifier.padding(12.dp)) }
        Text("只能访问你选择的目录。文本读取支持分页；单次新建或覆盖最多 12 KB，覆盖前保留备份。其他应用私有目录受 Android 限制。",style=MaterialTheme.typography.bodySmall)
        roots.forEach { (uri,label) -> Row(Modifier.fillMaxWidth()) {
            Text(label,Modifier.weight(1f).padding(vertical=12.dp))
            TextButton(onClick={runCatching {
                val grant=context.contentResolver.persistedUriPermissions.firstOrNull { it.uri==uri }
                if(grant!=null) context.contentResolver.releasePersistableUriPermission(uri,(if(grant.isReadPermission) Intent.FLAG_GRANT_READ_URI_PERMISSION else 0) or (if(grant.isWritePermission) Intent.FLAG_GRANT_WRITE_URI_PERMISSION else 0))
                revision++
            }.onFailure { model.notice="目录授权未能撤销，请重试。" }}) { Text("撤销") }
        } }
        OutlinedButton(enabled=roots.size<16,onClick={runCatching { picker.launch(Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION or Intent.FLAG_GRANT_PREFIX_URI_PERMISSION)) }.onFailure { model.notice="系统文件选择器不可用，请检查系统设置。" }}) { Text("选择授权目录") }
    }
}
