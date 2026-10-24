package ai.nexus.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Intent
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
class CoreIntegrationTest {
    @Test fun actualCoreLoginChatStopIndependentNodeAndNotifications() = runBlocking {
        val instrumentation=InstrumentationRegistry.getInstrumentation()
        val context=instrumentation.targetContext
        val file=File(context.filesDir,"android-fixture.json")
        assumeTrue("Run scripts/serve-fixture.py and push its isolated fixture first",file.exists())
        val fixture=wireJson.parseToJsonElement(file.readText()).jsonObject
        val client=CoreClient(Vault(context,"owner"))
        client.login(fixture.text("url"),fixture.text("owner_token"));client.checkSession()
        Vault(context,"node").write(obj("url" to str(fixture.text("url")),"node_id" to str(fixture.text("node_id")),"token" to str(fixture.text("token")),"packages" to fixture.getValue("packages")))
        val activity=instrumentation.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        // Only a test activity allows screenshots; release UI keeps FLAG_SECURE.
        instrumentation.runOnMainSync { activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE) }
        context.startForegroundService(Intent(context,NodeService::class.java))
        suspend fun waitFor(check: suspend () -> Boolean) = withTimeout(25000) { while(!check()) delay(250) }
        try {
            waitFor { client.request("GET","/api/v1/nodes").objects("nodes").any { it.text("online")=="true" && it.getValue("tools").toString().contains(NOTIFICATIONS) } }
            val create=obj("request_id" to str(uid()))
            val conversation=client.request("POST","/api/v1/chat/conversations",create).objectAt("conversation")
            val id=conversation.text("conversation_id")
            assertEquals(id,client.request("POST","/api/v1/chat/conversations",create).objectAt("conversation").text("conversation_id"))
            suspend fun chat(text: String): JsonObject {
                val history=client.request("GET","/api/v1/chat/conversations/$id/turns")
                return client.request("POST","/api/v1/chat/conversations/$id/turns",obj("request_id" to str(uid()),"expected_revision" to JsonPrimitive(history.objectAt("conversation").text("revision").toInt()),"text" to str(text),"allow_model" to JsonPrimitive(true))).objectAt("turn")
            }
            val turn=chat("测试本机连接")
            waitFor { client.request("GET","/api/v1/chat/turns/${turn.text("turn_id")}").objectAt("turn").text("status")=="completed" }
            assertTrue(client.request("GET","/api/v1/chat/turns/${turn.text("turn_id")}").objectAt("turn").objectAt("presentation").text("final_text").contains("本地测试替身"))
            val waiting=chat("等待")
            client.request("POST","/api/v1/chat/turns/${waiting.text("turn_id")}/stop",obj())
            waitFor { client.request("GET","/api/v1/chat/turns/${waiting.text("turn_id")}").objectAt("turn").text("status") in setOf("stopped","interrupted") }
            suspend fun task(tool: String): JsonObject {
                val request=obj("node_id" to str(fixture.text("node_id")),"request" to obj("request_id" to str(uid()),"intent" to str(tool.replace('.','_')),"arguments" to obj()))
                val result=client.request("POST","/api/v1/tasks",request).objectAt("task")
                assertEquals(result.text("task_id"),client.request("POST","/api/v1/tasks",request).objectAt("task").text("task_id"))
                return result
            }
            val device=task(DEVICE)
            waitFor { client.request("GET","/api/v1/tasks/${device.text("task_id")}").objectAt("task").text("status")=="succeeded" }
            val deviceDetail=client.request("GET","/api/v1/tasks/${device.text("task_id")}")
            assertEquals("node_report_schema_checked",deviceDetail.objectAt("task").text("verification"))
            assertEquals(1,deviceDetail.objects("attempts").size)
            val manager=context.getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(NotificationChannel("test","Integration fixture",NotificationManager.IMPORTANCE_DEFAULT))
            manager.notify(1001,Notification.Builder(context,"test").setSmallIcon(android.R.drawable.ic_dialog_info).setContentTitle("测试验证码").setContentText("PIN 852963").build())
            delay(500)
            val notification=task(NOTIFICATIONS)
            assertEquals("awaiting_approval",notification.text("status"))
            val detail=client.request("GET","/api/v1/tasks/${notification.text("task_id")}")
            assertTrue(detail.objects("attempts").isEmpty())
            client.request("POST","/api/v1/approvals/${detail.objects("approvals").first().text("approval_id")}",obj("decision" to str("approve")))
            waitFor { client.request("GET","/api/v1/tasks/${notification.text("task_id")}").objectAt("task").text("status")=="succeeded" }
            val final=client.request("GET","/api/v1/tasks/${notification.text("task_id")}")
            assertFalse(final.toString().contains("852963")); assertTrue(final.toString().contains("敏感通知已隐藏"))
            manager.cancel(1001)
            // Bound UI stays alive while STOP must terminate the foreground execution loop.
            context.startService(Intent(context,NodeService::class.java).setAction("STOP"))
            waitFor { client.request("GET","/api/v1/nodes").objects("nodes").none{it.text("online")=="true"} }
            instrumentation.runOnMainSync { activity.finish() }
        } finally {
            context.startService(Intent(context,NodeService::class.java).setAction("STOP"))
            client.http.dispatcher.cancelAll()
        }
    }
}
