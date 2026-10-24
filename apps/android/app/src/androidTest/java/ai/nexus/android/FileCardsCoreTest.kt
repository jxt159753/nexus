package ai.nexus.android

import android.content.Intent
import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModelProvider
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.ext.junit.runners.AndroidJUnit4
import ai.nexus.android.client.*
import ai.nexus.android.security.Vault
import ai.nexus.android.ui.*
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class FileCardsCoreTest {
    @Test fun authenticatedOriginalPreviewAndVisibleChatCard()=runBlocking {
        val i=InstrumentationRegistry.getInstrumentation();val context=i.targetContext
        val fixtureFile=File(context.filesDir,"android-fixture.json");assumeTrue(fixtureFile.exists())
        val fixture=wireJson.parseToJsonElement(fixtureFile.readText()).jsonObject
        val client=CoreClient(Vault(context,"owner"));client.login(fixture.text("url"),fixture.text("owner_token"))
        val conversation=client.request("POST","/api/v1/chat/conversations",obj("request_id" to str(uid()))).objectAt("conversation")
        val cid=conversation.text("conversation_id");val raw="真实文件原件\r\n手机可以预览与打开。".toByteArray()
        val item=client.request("POST","/api/v1/chat/conversations/$cid/attachments?request_id=${uid()}&filename=demo.txt",raw=raw).objectAt("attachment")
        val path="/api/v1/chat/conversations/$cid/files/attachment/${item.text("attachment_id")}/preview"
        val result=client.request("GET",path)
        assertEquals("text",result.objectAt("preview").text("kind"));assertEquals(String(raw),result.objectAt("preview").text("text"))
        assertArrayEquals(raw,client.fileBytes(item.text("download_url")))
        val activity=i.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        try {
            i.runOnMainSync {
                activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE)
                val model=ViewModelProvider(activity)[AppModel::class.java]
                activity.setContent { MaterialTheme(colorScheme=darkColorScheme()) { Surface(Modifier.fillMaxSize()) { Column(Modifier.padding(20.dp)) {
                    Markdown("## 文件已准备好\n\n**demo.txt** 保留原始内容，可预览或打开。\n\n- 大小和摘要已核对\n- 可以继续发送到授权设备")
                    ChatFileCards(model,listOf(result.objectAt("file")))
                } } } }
            }
            delay(3000);i.waitForIdleSync()
            val bitmap=requireNotNull(i.uiAutomation.takeScreenshot())
            File(context.getExternalFilesDir(null),"file-chat-card.png").outputStream().use{assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG,100,it))}
        } finally {i.runOnMainSync{activity.finish()};client.http.dispatcher.cancelAll()}
    }
}
