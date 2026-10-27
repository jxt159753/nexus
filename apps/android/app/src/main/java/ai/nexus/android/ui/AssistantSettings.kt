package ai.nexus.android.ui

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import ai.nexus.android.client.*
import kotlinx.serialization.json.JsonObject

@Composable internal fun AssistantSettings(model: AppModel) {
    LaunchedEffect(Unit) { model.loadAssistantSettings() }
    model.assistantSettings?.let { data ->
        val saved = data.objectAt("preferences")
        var personality by remember(saved) { mutableStateOf(saved.text("personality")) }
        var emoji by remember(saved) { mutableStateOf(saved.text("emoji")) }
        StyleChoice("选择人格",data.objects("personalities"),personality,!model.busy) { personality=it }
        val selected = data.objects("personalities").first { it.text("id")==personality }
        Text(selected.text("description"),style=MaterialTheme.typography.bodySmall,color=MaterialTheme.colorScheme.secondary)
        StyleChoice("回复中的 Emoji",data.objects("emoji_modes"),emoji,!model.busy) { emoji=it }
        Text("表达示例",style=MaterialTheme.typography.labelMedium)
        Surface(color=MaterialTheme.colorScheme.surfaceVariant,shape=MaterialTheme.shapes.medium) {
            Text(selected.objectAt("examples").text(emoji),modifier=Modifier.fillMaxWidth().padding(16.dp),style=MaterialTheme.typography.bodyMedium)
        }
        Text("保存到你的账户，各端共用，从下一轮对话生效。任务能力和权限不随人格改变。",style=MaterialTheme.typography.bodySmall,color=MaterialTheme.colorScheme.secondary)
        Button(onClick={model.saveAssistantSettings(personality,emoji,saved.getValue("revision"))},
            enabled=!model.busy && !model.assistantSettingsStale && (personality!=saved.text("personality") || emoji!=saved.text("emoji"))) { Text("保存表达偏好") }
    }
    if(model.assistantSettingsNotice.isNotEmpty()) Text(model.assistantSettingsNotice,style=MaterialTheme.typography.bodySmall)
    TextButton(onClick=model::loadAssistantSettings,enabled=!model.busy) { Text("读取最新设置") }
}

@Composable private fun StyleChoice(label: String, entries: List<JsonObject>, selected: String, enabled: Boolean, change: (String) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Column {
        Text(label,style=MaterialTheme.typography.labelMedium)
        Box {
            OutlinedButton(onClick={open=true},enabled=enabled,modifier=Modifier.fillMaxWidth()) {
                Text(entries.first { it.text("id")==selected }.text("name"))
            }
            DropdownMenu(expanded=open,onDismissRequest={open=false}) {
                entries.forEach { entry -> DropdownMenuItem(text={Text(entry.text("name"))},onClick={change(entry.text("id"));open=false}) }
            }
        }
    }
}
