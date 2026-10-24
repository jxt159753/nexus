package ai.nexus.android

import android.content.Intent
import android.graphics.Bitmap
import androidx.lifecycle.ViewModelProvider
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.ext.junit.runners.AndroidJUnit4
import ai.nexus.android.client.*
import ai.nexus.android.node.NodeService
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class FileReceiveCoreTest {
    @Test fun actualCoreUploadWebSocketReceiptAndInbox()=runBlocking {
        val instrumentation=InstrumentationRegistry.getInstrumentation();val context=instrumentation.targetContext
        val fixtureFile=File(context.filesDir,"android-fixture.json");assumeTrue(fixtureFile.exists())
        val fixture=wireJson.parseToJsonElement(fixtureFile.readText()).jsonObject
        val client=CoreClient(Vault(context,"owner"));client.login(fixture.text("url"),fixture.text("owner_token"))
        Vault(context,"node").write(obj("url" to str(fixture.text("url")),"node_id" to str(fixture.text("node_id")),"token" to str(fixture.text("token")),"packages" to JsonArray(emptyList()),"receive_files" to JsonPrimitive(true)))
        val activity=instrumentation.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        context.startForegroundService(Intent(context,NodeService::class.java))
        suspend fun waitFor(check:suspend ()->Boolean)=withTimeout(25000){while(!check())delay(200)}
        var received:File?=null
        try {
            waitFor{client.request("GET","/api/v1/file-transfers/targets").objects("nodes").any{it.text("available")=="true"}}
            val conversation=client.request("POST","/api/v1/chat/conversations",obj("request_id" to str(uid()))).objectAt("conversation")
            val cid=conversation.text("conversation_id");val raw=ByteArray(4*1024*1024+17){(it%256).toByte()}
            val item=client.request("POST","/api/v1/chat/conversations/$cid/attachments?request_id=${uid()}&filename=android-roundtrip.bin",raw=raw).objectAt("attachment")
            val request=obj("request_id" to str(uid()),"node_id" to str(fixture.text("node_id")))
            val path="/api/v1/chat/conversations/$cid/attachments/${item.text("attachment_id")}/transfers"
            val task=client.request("POST",path,request).objectAt("task")
            var detail=obj()
            waitFor{detail=client.request("GET","/api/v1/tasks/${task.text("task_id")}");detail.objectAt("task").text("status") in setOf("succeeded","failed","timed_out")}
            assertEquals(detail.toString(),"succeeded",detail.objectAt("task").text("status"))
            val result=detail.objectAt("task").objectAt("result")
            received=File(context.filesDir,"received/${result.text("path")}");assertArrayEquals(raw,received!!.readBytes())
            assertEquals(item.text("sha256"),result.text("sha256"));assertEquals(1,detail.objects("attempts").size)
            assertEquals(task.text("task_id"),client.request("POST",path,request).objectAt("task").text("task_id"))
            instrumentation.runOnMainSync {
                activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE)
                val model=ViewModelProvider(activity)[AppModel::class.java];model.history=obj("conversation" to conversation);model.page=2;model.refreshAll()
            }
            delay(1500);instrumentation.waitForIdleSync()
            val bitmap=requireNotNull(instrumentation.uiAutomation.takeScreenshot())
            File(context.getExternalFilesDir(null),"file-transfer.png").outputStream().use{assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG,100,it))}
        } finally {
            context.startService(Intent(context,NodeService::class.java).setAction("STOP"));instrumentation.runOnMainSync{activity.finish()}
            received?.delete();received?.parentFile?.delete();client.http.dispatcher.cancelAll()
        }
    }
}
