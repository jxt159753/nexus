package ai.nexus.android.ui

import android.content.Intent
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

/** Keep already received originals accessible after removing the transfer form. */
@Composable fun ReceivedFiles() {
    val context=LocalContext.current
    var open by remember { mutableStateOf(false) }
    var files by remember { mutableStateOf(emptyList<File>()) }
    var notice by remember { mutableStateOf("") }
    LaunchedEffect(open) {
        if(open) files=withContext(Dispatchers.IO) { File(context.filesDir,"received/NEXUS-Inbox").listFiles().orEmpty().asSequence()
            .filter{it.isDirectory}.flatMap{it.listFiles().orEmpty().asSequence()}.filter{it.isFile && !it.name.startsWith(".receive-")}
            .sortedByDescending{it.lastModified()}.take(100).toList() }
    }
    TextButton(onClick={open=!open}) { Text(if(open) "收起收到的文件" else "收到的文件") }
    if(open) Column(verticalArrangement=Arrangement.spacedBy(8.dp)) {
        if(files.isEmpty()) Text("还没有收到文件。",style=MaterialTheme.typography.bodySmall)
        files.forEach { file -> TextButton(onClick={runCatching {
            val uri=FileProvider.getUriForFile(context,"${context.packageName}.received",file)
            val type=android.webkit.MimeTypeMap.getSingleton().getMimeTypeFromExtension(file.extension.lowercase()) ?: "application/octet-stream"
            context.startActivity(Intent.createChooser(Intent(Intent.ACTION_VIEW).setDataAndType(uri,type).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION),"打开收到的文件"))
        }.onFailure{notice="没有可打开此文件的应用，原件仍保存在本机。"}}){Text("${file.name} · ${fileSize(file.length().toString())}")} }
        if(notice.isNotEmpty()) Text(notice)
    }
}
