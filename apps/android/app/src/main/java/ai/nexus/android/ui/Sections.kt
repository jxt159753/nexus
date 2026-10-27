package ai.nexus.android.ui

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable internal fun SectionCard(title: String, content: @Composable ColumnScope.() -> Unit) {
    OutlinedCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp),verticalArrangement=Arrangement.spacedBy(12.dp)) {
            Text(title,style=MaterialTheme.typography.titleMedium)
            content()
        }
    }
}

@Composable internal fun Disclosure(title: String, summary: String = "", content: @Composable ColumnScope.() -> Unit) {
    var open by rememberSaveable { mutableStateOf(false) }
    OutlinedCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp),verticalArrangement=Arrangement.spacedBy(8.dp)) {
            TextButton(onClick={open=!open},contentPadding=PaddingValues(0.dp),modifier=Modifier.fillMaxWidth()) {
                Row(Modifier.fillMaxWidth(),verticalAlignment=Alignment.CenterVertically) {
                    Text(title,Modifier.weight(1f),style=MaterialTheme.typography.titleMedium)
                    Text(if(open) "收起" else "展开",style=MaterialTheme.typography.labelMedium)
                }
            }
            if(summary.isNotEmpty()) Text(summary,style=MaterialTheme.typography.bodySmall,color=MaterialTheme.colorScheme.secondary)
            if(open) content()
        }
    }
}
