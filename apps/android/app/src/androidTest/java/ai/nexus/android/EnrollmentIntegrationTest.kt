package ai.nexus.android

import android.content.Intent
import androidx.lifecycle.ViewModelProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import ai.nexus.android.client.*
import ai.nexus.android.node.NodeService
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** Dedicated, freshly reset emulator only; synthetic owner and real Core HTTP/WS. */
@RunWith(AndroidJUnit4::class)
class EnrollmentIntegrationTest {
    @Test fun registeredScreens(): Unit = runBlocking {
        val test=InstrumentationRegistry.getInstrumentation();val context=test.targetContext
        check(Vault(context,"node").read()!=null)
        val activity=test.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        lateinit var model:AppModel
        test.runOnMainSync { activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE);model=ViewModelProvider(activity)[AppModel::class.java] }
        withTimeout(30000) { while(!model.signedIn || model.busy || model.localDevice.text("registered")!="true") delay(150) }
        test.runOnMainSync { model.page=2 }
        delay(1200)
        File(context.filesDir,"enrollment-device.png").outputStream().use { test.uiAutomation.takeScreenshot().compress(android.graphics.Bitmap.CompressFormat.PNG,100,it) }
        test.runOnMainSync { model.page=0;model.selectConversation(model.conversations.first().text("conversation_id")) }
        withTimeout(30000) { while(model.busy) delay(150) }
        delay(1200)
        File(context.filesDir,"enrollment-chat.png").outputStream().use { test.uiAutomation.takeScreenshot().compress(android.graphics.Bitmap.CompressFormat.PNG,100,it) }
        test.runOnMainSync { activity.finish() }
    }
    @Test fun oneSentenceInstallsIdentityStartsNodeAndEditsPermissions(): Unit = runBlocking {
        val test = InstrumentationRegistry.getInstrumentation()
        val context = test.targetContext
        check(Vault(context,"node").read() == null) { "Use a fresh dedicated emulator app profile" }
        val fixture = wireJson.parseToJsonElement(File(context.filesDir,"android-fixture.json").readText()).jsonObject
        val client = CoreClient(Vault(context,"owner"))
        client.login(fixture.text("url"),fixture.text("owner_token"))
        val activity = test.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        lateinit var model: AppModel
        test.runOnMainSync { activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE); model=ViewModelProvider(activity)[AppModel::class.java] }
        suspend fun waitFor(check: () -> Boolean) = withTimeout(30000) { while(!check()) delay(100) }
        suspend fun send(text: String) {
            waitFor { !model.busy && model.signedIn }
            val old = model.history.objects("turns").size
            test.runOnMainSync { model.draft=text; model.allowModel=true; model.allowTools=false; model.send() }
            waitFor { !model.busy && model.history.objects("turns").size > old }
        }
        try {
            send("将此设备注册为节点，命名为测试平板，允许设备状态、接收文件和语音")
            val saved = requireNotNull(Vault(context,"node").read())
            val id = saved.text("node_id")
            assertNotEquals(fixture.text("node_id"),id)
            withTimeout(30000) { while(client.request("GET","/api/v1/nodes").objects("nodes").none { it.text("node_id")==id && it.text("online")=="true" }) delay(200) }
            assertEquals("ready",client.request("GET","/api/v1/file-transfers/targets").objects("nodes").first { it.text("node_id")==id }.text("reason"))
            val task=client.request("POST","/api/v1/tasks",obj("node_id" to str(id),"request" to obj("request_id" to str(uid()),"intent" to str("android_device_status"),"arguments" to obj()))).objectAt("task")
            withTimeout(30000) { while(client.request("GET","/api/v1/tasks/${task.text("task_id")}").objectAt("task").text("status")!="succeeded") delay(200) }
            send("将测试平板改名为书房平板")
            send("将书房平板的备注设为书桌上的测试设备")
            send("关闭书房平板的接收文件权限")
            assertFalse(model.localDevice.objectAt("permissions").getValue("receive_files").jsonPrimitive.boolean)
            assertEquals("书桌上的测试设备",model.localDevice.text("description"))
            send("将此设备注册为节点")
            assertEquals(id,Vault(context,"node").read()!!.text("node_id"))
            assertEquals(saved.text("token"),Vault(context,"node").read()!!.text("token"))
            assertEquals(1,client.request("GET","/api/v1/nodes").objects("nodes").count { it.text("node_id")==id })
            assertTrue(model.history.objects("turns").all { it.text("model_call_count")=="0" })
            test.runOnMainSync { model.page=2 }
            delay(800)
            test.uiAutomation.takeScreenshot().compress(android.graphics.Bitmap.CompressFormat.PNG,100,File(context.filesDir,"enrollment-device.png").outputStream())
            test.runOnMainSync { model.page=0 }
            delay(800)
            test.uiAutomation.takeScreenshot().compress(android.graphics.Bitmap.CompressFormat.PNG,100,File(context.filesDir,"enrollment-chat.png").outputStream())
        } finally {
            context.startService(Intent(context,NodeService::class.java).setAction("STOP"))
            test.runOnMainSync { activity.finish() }
            client.http.dispatcher.cancelAll()
        }
    }
}
