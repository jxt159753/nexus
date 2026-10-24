package ai.nexus.android.ui

import android.content.Intent
import android.graphics.BitmapFactory
import android.util.Base64
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import ai.nexus.android.client.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import java.io.File
import java.security.MessageDigest

@Composable fun ChatFileCards(model:AppModel,files:List<JsonObject>) {
    val context=LocalContext.current
    val scope=rememberCoroutineScope()
    var preview by remember { mutableStateOf<JsonObject?>(null) }
    var previewName by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    files.take(8).forEach { file ->
        Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(14.dp),verticalArrangement=Arrangement.spacedBy(6.dp)) {
            Text(file.text("filename"),style=MaterialTheme.typography.titleSmall)
            Text("${fileSize(file.text("size_bytes"))} · ${statusLabel(file.text("status"))}",style=MaterialTheme.typography.bodySmall)
            Row {
                TextButton(enabled=!busy,onClick={scope.launch {
                    busy=true
                    try { preview=model.client.request("GET",file.text("preview_url")).objectAt("preview");previewName=file.text("filename") }
                    catch(error:Exception){model.notice=error.message ?: "文件预览暂不可用。"}
                    finally{busy=false}
                }}) { Text("预览") }
                TextButton(enabled=!busy,onClick={scope.launch {
                    busy=true
                    try {
                        val bytes=model.client.fileBytes(file.text("download_url"))
                        val saved=withContext(Dispatchers.IO) {
                            val sha=MessageDigest.getInstance("SHA-256").digest(bytes).joinToString(""){"%02x".format(it)}
                            require(bytes.size.toString()==file.text("size_bytes") && sha==file.text("sha256")){"文件完整性校验失败"}
                            val root=File(context.cacheDir,"file-preview").apply{mkdirs()}
                            // Cache only user-opened originals, bounded independently of the receiving inbox.
                            root.listFiles().orEmpty().filter{it.isDirectory}.sortedBy{it.lastModified()}.dropLast(1).forEach{it.deleteRecursively()}
                            val folder=File(root,sha).apply{mkdirs()}
                            val name=file.text("filename").replace(Regex("[\\\\/:*?\"<>|\\p{Cntrl}]"),"_").take(120).ifBlank{"file"}
                            File(folder,name).apply{writeBytes(bytes)}
                        }
                        val uri=FileProvider.getUriForFile(context,"${context.packageName}.received",saved)
                        val type=android.webkit.MimeTypeMap.getSingleton().getMimeTypeFromExtension(saved.extension.lowercase()) ?: "application/octet-stream"
                        context.startActivity(Intent.createChooser(Intent(Intent.ACTION_VIEW).setDataAndType(uri,type).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION),"打开文件"))
                    } catch(error:Exception){model.notice=error.message ?: "没有可打开此文件的应用。"}
                    finally{busy=false}
                }}) { Text("打开原件") }
            }
        } }
    }
    preview?.let { value -> AlertDialog(onDismissRequest={preview=null},title={Text(previewName)},text={Column(Modifier.heightIn(max=440.dp).verticalScroll(rememberScrollState())) {
        when(value.text("kind")) {
            "text" -> { SelectionContainer{Text(value.text("text"))};if(value.text("truncated")=="true")Text("预览已截断，打开原件可查看全部内容。") }
            "image" -> {
                val image=value.objectAt("snapshot").objectAt("image")
                val bitmap=remember(image){runCatching{Base64.decode(image.text("data_base64"),Base64.DEFAULT).let{BitmapFactory.decodeByteArray(it,0,it.size)}}.getOrNull()}
                bitmap?.let{Image(it.asImageBitmap(),previewName,Modifier.fillMaxWidth())}
            }
            else -> Text("此格式暂不支持内嵌预览，请使用‘打开原件’选择相应应用。")
        }
    }},confirmButton={TextButton(onClick={preview=null}){Text("关闭")}}) }
}
