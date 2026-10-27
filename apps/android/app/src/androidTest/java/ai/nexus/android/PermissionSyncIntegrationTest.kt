package ai.nexus.android

import android.content.Intent
import android.view.accessibility.AccessibilityNodeInfo
import androidx.lifecycle.ViewModelProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import ai.nexus.android.client.*
import ai.nexus.android.node.PhoneAccess
import ai.nexus.android.node.NodeService
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** Fresh dedicated emulator and temporary Core, never a real phone or production identity. */
@RunWith(AndroidJUnit4::class)
class PermissionSyncIntegrationTest {
    @Test fun persistedChoicesAfterProcessRestart(): Unit = runBlocking {
        val test=InstrumentationRegistry.getInstrumentation();val context=test.targetContext
        val local=requireNotNull(Vault(context,"node").read())
        val saved=requireNotNull(Vault(context,"device-client").read()).objectAt("permission_sync")
        val client=CoreClient(Vault(context,"owner"));assertTrue(client.restore())
        val result=DeviceEnrollment(context,client).sync();assertNull(result.error);assertFalse(result.installed)
        assertEquals(local.text("node_id"),result.status.text("node_id"))
        assertEquals(saved,result.status.objectAt("permission_sync"))
        assertFalse(PhoneAccess(context).enabled("accessibility"))
        assertTrue(PhoneAccess(context).enabled("files"));assertTrue(PhoneAccess(context).enabled("vision"))
        val activity=test.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        lateinit var model:AppModel
        test.runOnMainSync { model=ViewModelProvider(activity)[AppModel::class.java];activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE);model.page=2 }
        try {
            withTimeout(30000) { while(model.localDevice.objectAt("permission_sync")!=saved) delay(100) }
            delay(400)
            File(context.filesDir,"permission-sync-restored.png").outputStream().use { test.uiAutomation.takeScreenshot().compress(android.graphics.Bitmap.CompressFormat.PNG,100,it) }
        } finally { test.runOnMainSync { activity.finish() };client.http.dispatcher.cancelAll() }
    }

    @Test fun nativeSwitchesSyncAndPendingChoicesSurviveRestart(): Unit = runBlocking {
        val test=InstrumentationRegistry.getInstrumentation(); val context=test.targetContext
        check(Vault(context,"node").read()==null) { "Use a fresh dedicated test app" }
        val fixture=wireJson.parseToJsonElement(File(context.filesDir,"android-fixture.json").readText()).jsonObject
        val client=CoreClient(Vault(context,"owner"))
        client.login(fixture.text("url"),fixture.text("owner_token"))
        val activity=test.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        lateinit var model:AppModel
        test.runOnMainSync { model=ViewModelProvider(activity)[AppModel::class.java];activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE) }
        suspend fun waitFor(check:()->Boolean)=withTimeout(30000) { while(!withContext(Dispatchers.Main) { check() }) delay(100) }
        suspend fun send(text:String) {
            waitFor { model.signedIn && !model.busy }
            val count=model.history.objects("turns").size
            test.runOnMainSync { model.draft=text;model.allowModel=true;model.allowTools=false;model.send() }
            waitFor { !model.busy && model.history.objects("turns").size>count }
        }
        fun nodes(node:AccessibilityNodeInfo?):List<AccessibilityNodeInfo> = if(node==null) emptyList() else listOf(node)+(0 until node.childCount).flatMap { nodes(node.getChild(it)) }
        val access=PhoneAccess(context)
        try {
            send("注册这台手机，叫测试手机")
            assertTrue(model.history.objects("turns").last().text("response_text").contains("你希望开启哪些功能"))
            assertFalse(model.localDevice.objectAt("permissions").getValue("receive_files").jsonPrimitive.boolean)
            send("语音和接收文件")
            assertTrue(model.localDevice.objectAt("permissions").text("voice")=="true")
            withTimeout(30000) {
                while(client.request("GET","/api/v1/file-transfers/targets").objects("nodes").none { it.text("node_id")==model.localDevice.text("node_id") && it.text("reason")=="ready" }) delay(200)
            }
            send("这台手机改名叫同步测试手机")
            assertEquals("同步测试手机",model.localDevice.text("name"))
            test.runOnMainSync { model.page=2 }
            // Click the actual Compose switch. No system accessibility grant is made.
            withTimeout(15000) {
                while(true) {
                    val all=nodes(test.uiAutomation.rootInActiveWindow)
                    val switch=all.firstOrNull { it.isCheckable && it.isVisibleToUser }
                    if(switch!=null) { check(switch.performAction(AccessibilityNodeInfo.ACTION_CLICK));break }
                    all.firstOrNull { it.isScrollable }?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
                    delay(300)
                }
            }
            waitFor { access.enabled("accessibility") && model.localDevice.objectAt("permissions").text("accessibility")=="true" }
            assertTrue(model.deviceSyncNotice.contains("已同步"))
            val id=model.localDevice.text("node_id")
            val listed=client.request("GET","/api/v1/nodes").objects("nodes").first { it.text("node_id")==id }
            assertFalse(listed.getValue("tools").jsonArray.any { it.jsonPrimitive.content=="android.ui_act@1" })
            // An offline owner client cannot send; the local off remains durable.
            val offline=CoreClient(Vault(context,"owner"));assertTrue(offline.restore())
            val fresh=DeviceEnrollment(context,offline)
            test.runOnMainSync { model.foreground(false) }
            File(context.filesDir,"permission-offline.request").writeText("ready")
            withTimeout(20000) { while(!File(context.filesDir,"permission-offline.ready").exists()) delay(100) }
            access.set("accessibility",false)
            assertFalse(access.enabled("accessibility"))
            assertTrue(runCatching { withTimeout(5000) { fresh.sync() } }.isFailure)
            File(context.filesDir,"permission-offline.done").writeText("done")
            withTimeout(20000) { while(!File(context.filesDir,"permission-online.ready").exists()) delay(100) }
            // Recreate the sync client, as after process restart, then read back Core.
            val restored=CoreClient(Vault(context,"owner"));assertTrue(restored.restore())
            val enrollment=DeviceEnrollment(context,restored)
            val off=enrollment.sync();assertNull(off.error)
            assertFalse(off.status.objectAt("permissions").getValue("accessibility").jsonPrimitive.boolean)
            access.set("files",true);access.set("vision",true)
            val on=enrollment.sync();assertNull(on.error)
            assertTrue(on.status.objectAt("permissions").getValue("files").jsonPrimitive.boolean)
            assertTrue(on.status.objectAt("permissions").getValue("vision").jsonPrimitive.boolean)
            val snapshot=on.status.objectAt("permission_sync")
            assertEquals(snapshot,DeviceEnrollment(context,restored).sync().status.objectAt("permission_sync"))
            test.runOnMainSync { model.foreground(true) }
            waitFor { model.localDevice.objectAt("permission_sync")==snapshot }
            File(context.filesDir,"permission-sync.png").outputStream().use { test.uiAutomation.takeScreenshot().compress(android.graphics.Bitmap.CompressFormat.PNG,100,it) }
            assertTrue(model.history.objects("turns").all { it.text("model_call_count")=="0" })
        } finally {
            context.startService(Intent(context,NodeService::class.java).setAction("STOP"))
            test.runOnMainSync { activity.finish() };client.http.dispatcher.cancelAll()
        }
    }
}
