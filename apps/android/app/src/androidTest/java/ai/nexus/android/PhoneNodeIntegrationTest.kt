package ai.nexus.android

import android.app.UiAutomation
import android.content.ComponentName
import android.content.Intent
import android.net.Uri
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.ext.junit.runners.AndroidJUnit4
import ai.nexus.android.client.*
import ai.nexus.android.node.*
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class PhoneNodeIntegrationTest {
    @Test fun phonePermissionControlsFitScrollablePage() = runBlocking {
        val test=InstrumentationRegistry.getInstrumentation();val context=test.targetContext
        val automation=test.uiAutomation
        val fixture=wireJson.parseToJsonElement(File(context.filesDir,"phone-fixture.json").readText()).jsonObject
        CoreClient(Vault(context,"owner")).apply { login(fixture.text("url"),fixture.text("owner_token"));http.dispatcher.cancelAll() }
        val activity=test.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        fun nodes(node: android.view.accessibility.AccessibilityNodeInfo?): List<android.view.accessibility.AccessibilityNodeInfo> {
            if(node==null || !node.refresh()) return emptyList()
            return listOf(node)+(0 until node.childCount).flatMap { nodes(node.getChild(it)) }
        }
        fun all()=nodes(automation.rootInActiveWindow)
        try {
            lateinit var model: AppModel
            test.runOnMainSync { model=androidx.lifecycle.ViewModelProvider(activity)[AppModel::class.java] }
            withTimeout(10000) { while(model.busy) delay(100) }
            test.runOnMainSync { model.foreground(false);model.page=2 }
            assertTrue("Local fixture login must succeed",model.signedIn)
            withTimeout(10000) { while(all().none { it.text?.toString()=="手机操作与文件" }) delay(100) }
            var scroller=all().first { it.text?.toString()=="手机操作与文件" }
            while(!scroller.isScrollable) scroller=requireNotNull(scroller.parent)
            assertTrue(scroller.performAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_SCROLL_FORWARD))
            delay(700)
            assertTrue(all().any { it.text?.toString()=="选择授权目录" && it.isVisibleToUser })
            File(context.getExternalFilesDir(null),"phone-permissions-controls.png").outputStream().use {
                check(test.uiAutomation.takeScreenshot().compress(android.graphics.Bitmap.CompressFormat.PNG,100,it))
            }
        } catch(error: Throwable) {
            println("PHONE_UI visible="+all().mapNotNull { it.text }.joinToString())
            automation.takeScreenshot()?.let { image -> File(context.getExternalFilesDir(null),"phone-permissions-failure.png").outputStream().use { image.compress(android.graphics.Bitmap.CompressFormat.PNG,100,it) } }
            throw error
        } finally { test.runOnMainSync { activity.finish() } }
    }

    @Test fun authenticatedPhoneUiAndDocuments() = runBlocking {
        val instrumentation=InstrumentationRegistry.getInstrumentation();val context=instrumentation.targetContext
        val fixture=wireJson.parseToJsonElement(File(context.filesDir,"phone-fixture.json").readText()).jsonObject
        val client=CoreClient(Vault(context,"owner"));client.login(fixture.text("url"),fixture.text("owner_token"))
        Vault(context,"node").write(obj("url" to fixture.getValue("url"),"node_id" to fixture.getValue("node_id"),"token" to fixture.getValue("token"),"packages" to JsonArray(emptyList()),"receive_files" to JsonPrimitive(false)))
        val local=PhoneAccess(context);local.set("files",true);local.set("accessibility",true)
        val automation=instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)
        val main=instrumentation.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        try {
            context.startForegroundService(Intent(context,NodeService::class.java))
            suspend fun waitFor(label: String, check: suspend () -> Boolean) {
                check(withTimeoutOrNull(25000) { while(!check()) delay(200);true }==true) { "Timeout: $label" }
            }
            fun open() {
                context.startActivity(Intent().setComponent(ComponentName("ai.nexus.android.test","ai.nexus.android.PhoneFixtureActivity")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
            }
            // Only this dedicated emulator is configured for the test service.
            automation.executeShellCommand("settings put secure enabled_accessibility_services ai.nexus.android/ai.nexus.android.node.PhoneAccessibility").close()
            automation.executeShellCommand("settings put secure accessibility_enabled 1").close()
            open()
            waitFor("fixture window") { automation.rootInActiveWindow?.packageName?.toString()=="ai.nexus.android.test" }
            val tree=Uri.parse("content://ai.nexus.android.test.documents/tree/root")
            context.contentResolver.takePersistableUriPermission(tree,Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            val node=fixture.text("node_id")
            suspend fun advertised() = client.request("GET","/api/v1/nodes").objects("nodes").single { it.text("node_id")==node }
            suspend fun connected() = advertised().let { it.text("online")=="true" && listOf(UI_ACT,FILE_WRITE).all { tool -> it.getValue("tools").toString().contains(tool) } }
            try { waitFor("Node registration with accessibility") { connected() } }
            catch(failure: Exception) {
                println("PHONE_TEST nodes="+client.request("GET","/api/v1/nodes"))
                android.os.ParcelFileDescriptor.AutoCloseInputStream(automation.executeShellCommand("dumpsys activity services ai.nexus.android")).use { println(it.bufferedReader().readText()) }
                throw failure
            }
            var submitted=0
            suspend fun task(tool: String,args: JsonObject): JsonObject {
                if(submitted==8) {
                    // Let the entire previous batch leave the sliding HTTP rate window.
                    delay(61000);submitted=0
                }
                submitted++
                val request=obj("node_id" to str(node),"request" to obj("request_id" to str(uid()),"intent" to str(tool.replace('.','_')),"arguments" to args))
                val created=try { client.request("POST","/api/v1/tasks",request).objectAt("task") }
                    catch(failure: ApiFailure) { throw AssertionError("Submit $tool: ${failure.status} ${failure.code}",failure) }
                val id=created.text("task_id")
                var detail=client.request("GET","/api/v1/tasks/$id")
                if(created.text("status")=="awaiting_approval") try { client.request("POST","/api/v1/approvals/${detail.objects("approvals").last().text("approval_id")}",obj("decision" to str("approve"))) }
                    catch(failure: ApiFailure) { throw AssertionError("Approve $tool: ${failure.status} ${failure.code}",failure) }
                waitFor("task $tool $id") { detail=client.request("GET","/api/v1/tasks/$id");detail.objectAt("task").text("status") in setOf("succeeded","failed","cancelled","timed_out") }
                assertEquals(id,client.request("POST","/api/v1/tasks",request).objectAt("task").text("task_id"))
                return detail
            }
            fun output(detail: JsonObject): JsonObject {
                assertEquals(detail.toString(),"succeeded",detail.objectAt("task").text("status"))
                return detail.objectAt("task").objectAt("result")
            }
            suspend fun observe()=output(task(UI_OBSERVE,obj()))
            var screen=observe()
            assertTrue(screen.toString().contains("Phone test click"));assertFalse(screen.toString().contains("private-password"))
            assertTrue(screen.objectAt("state").objects("nodes").single { it.text("text")=="Phone test initial" }.text("description").endsWith("🙂"))
            val click=screen.objectAt("state").objects("nodes").single { it.text("text")=="Phone test click" }
            val action=output(task(UI_ACT,obj("observation_digest" to screen.getValue("observation_digest"),"action" to str("click"),"node" to click.getValue("node"))))
            assertEquals("action_returned",action.text("verification"))
            screen=observe();assertTrue(screen.toString().contains("Phone test clicked"))
            val stale=task(UI_ACT,obj("observation_digest" to str("0".repeat(64)),"action" to str("home")))
            assertEquals("failed",stale.objectAt("task").text("status"))
            screen=observe();val input=screen.objectAt("state").objects("nodes").single { it.text("description")=="Phone test input" }
            output(task(UI_ACT,obj("observation_digest" to screen.getValue("observation_digest"),"action" to str("set_text"),"node" to input.getValue("node"),"text" to str("输入成功🙂"))))
            assertTrue(observe().toString().contains("输入成功🙂"))
            val roots=output(task(FILES,obj("operation" to str("roots")))).objects("entries")
            val rootId=roots.single().text("tree_id")
            suspend fun read(path: String)=output(task(FILES,obj("operation" to str("read"),"tree_id" to str(rootId),"path" to str(path))))
            assertEquals("old phone content",read("existing.txt").text("text"))
            val args=obj("tree_id" to str(rootId),"path" to str("existing.txt"),"content" to str("新手机内容🙂"))
            val preview=task(FILE_PREVIEW,args);val evidence=output(preview)
            val written=output(task(FILE_WRITE,JsonObject(args+mapOf("expected_sha256" to evidence.getValue("previous_sha256"),"preview_digest" to evidence.getValue("preview_digest"),"preview_task_id" to preview.objectAt("task").getValue("task_id")))))
            assertEquals("新手机内容🙂",read("existing.txt").text("text"))
            assertEquals("old phone content",read(written.text("backup_path")).text("text"))
            val create=obj("tree_id" to str(rootId),"path" to str("created.txt"),"content" to str("created"))
            val cp=task(FILE_PREVIEW,create);val ce=output(cp)
            output(task(FILE_WRITE,JsonObject(create+mapOf("expected_sha256" to JsonNull,"preview_digest" to ce.getValue("preview_digest"),"preview_task_id" to cp.objectAt("task").getValue("task_id")))))
            assertEquals("created",read("created.txt").text("text"))
            // Reusing an old preview cannot overwrite a newly changed file.
            val staleWrite=task(FILE_WRITE,JsonObject(args+mapOf("expected_sha256" to evidence.getValue("previous_sha256"),"preview_digest" to evidence.getValue("preview_digest"),"preview_task_id" to preview.objectAt("task").getValue("task_id"))))
            assertEquals("failed",staleWrite.objectAt("task").text("status"))
            context.contentResolver.releasePersistableUriPermission(tree,Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            waitFor("file grant capability reduction") { advertised().let { it.text("online")=="true" && !it.getValue("tools").toString().contains(FILE_WRITE) } }
            val revoked=task(FILES,obj("operation" to str("read"),"tree_id" to str(rootId),"path" to str("existing.txt")))
            assertEquals("failed",revoked.objectAt("task").text("status"))
            // Regression: chat updates must not invalidate HOME/RECENTS. BACK
            // remains contextual, and a different foreground window is rejected.
            fun showChat(notice: String) {
                context.startActivity(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT))
                instrumentation.runOnMainSync {
                    val model=androidx.lifecycle.ViewModelProvider(main as MainActivity)[AppModel::class.java]
                    model.foreground(false);model.page=0;model.notice=notice
                }
            }
            fun contains(node: android.view.accessibility.AccessibilityNodeInfo?, text: String): Boolean =
                node!=null && (node.text?.toString()?.contains(text)==true || (0 until node.childCount).any { contains(node.getChild(it),text) })
            showChat("navigation before")
            waitFor("chat before navigation") { contains(automation.rootInActiveWindow?.also { it.refresh() },"navigation before") }
            val navigation=observe()
            showChat("navigation after streaming update")
            waitFor("changed chat text") { contains(automation.rootInActiveWindow?.also { it.refresh() },"navigation after streaming update") }
            val staleBack=task(UI_ACT,obj("observation_digest" to navigation.getValue("observation_digest"),"action" to str("back")))
            assertEquals("failed",staleBack.objectAt("task").text("status"))
            output(task(UI_ACT,obj("observation_digest" to navigation.getValue("observation_digest"),"action" to str("home"))))
            waitFor("HOME actually leaves NEXUS") { automation.rootInActiveWindow?.packageName?.toString()?.let { it!=context.packageName }==true }
            println("PHONE_NAV HOME foreground="+automation.rootInActiveWindow?.packageName)
            File(context.getExternalFilesDir(null),"phone-home-regression.png").outputStream().use {
                check(automation.takeScreenshot().compress(android.graphics.Bitmap.CompressFormat.PNG,100,it))
            }
            showChat("recents before")
            waitFor("chat before recents") { contains(automation.rootInActiveWindow?.also { it.refresh() },"recents before") }
            val recents=observe()
            showChat("recents after streaming update")
            waitFor("changed chat before recents") { contains(automation.rootInActiveWindow?.also { it.refresh() },"recents after streaming update") }
            output(task(UI_ACT,obj("observation_digest" to recents.getValue("observation_digest"),"action" to str("recents"))))
            waitFor("RECENTS actually leaves NEXUS") { automation.rootInActiveWindow?.packageName?.toString()?.let { it!=context.packageName }==true }
            println("PHONE_NAV RECENTS foreground="+automation.rootInActiveWindow?.packageName)
            showChat("window binding before")
            waitFor("chat before window switch") { contains(automation.rootInActiveWindow?.also { it.refresh() },"window binding before") }
            val priorWindow=observe()
            open();waitFor("different foreground window") { automation.rootInActiveWindow?.packageName?.toString()=="ai.nexus.android.test" }
            val wrongWindow=task(UI_ACT,obj("observation_digest" to priorWindow.getValue("observation_digest"),"action" to str("home")))
            assertEquals("failed",wrongWindow.objectAt("task").text("status"))
            assertEquals("ai.nexus.android.test",automation.rootInActiveWindow?.packageName?.toString())
            local.set("vision",true)
            waitFor("screen tools registered") { advertised().getValue("tools").toString().contains(SCREEN_ACT) }
            context.startActivity(Intent().setComponent(ComponentName("ai.nexus.android.test","ai.nexus.android.PhoneScreenFixtureActivity")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
            waitFor("canvas fixture") { automation.rootInActiveWindow?.packageName?.toString()=="ai.nexus.android.test" }
            delay(700)
            assertFalse(observe().toString().contains("Tap target"))
            fun imageBytes(value: JsonObject)=android.util.Base64.decode(value.objectAt("image").text("data_base64"),android.util.Base64.DEFAULT)
            fun act(value: JsonObject,action: String,x: Int,y: Int,endX: Int?=null,endY: Int?=null): JsonObject {
                val state=value.objectAt("state");val image=value.objectAt("image");val region=state.getValue("region").jsonArray.map { it.jsonPrimitive.int }
                fun px(v: Int)=JsonPrimitive(((v-region[0]).toDouble()*image.text("width").toInt()/(region[2]-region[0])).toInt())
                fun py(v: Int)=JsonPrimitive(((v-region[1]).toDouble()*image.text("height").toInt()/(region[3]-region[1])).toInt())
                return obj("observation_digest" to value.getValue("observation_digest"),"action" to str(action),"x" to px(x),"y" to py(y),"end_x" to (endX?.let(::px) ?: JsonNull),"end_y" to (endY?.let(::py) ?: JsonNull))
            }
            fun color(value: JsonObject,x: Int,y: Int,expected: Int) {
                val raw=imageBytes(value);val bitmap=android.graphics.BitmapFactory.decodeByteArray(raw,0,raw.size)
                try {
                    val state=value.objectAt("state");val pixel=bitmap.getPixel(x*bitmap.width/state.text("width").toInt(),y*bitmap.height/state.text("height").toInt())
                    assertTrue("Canvas color $pixel must approximate $expected",listOf(android.graphics.Color.red(pixel)-android.graphics.Color.red(expected),android.graphics.Color.green(pixel)-android.graphics.Color.green(expected),android.graphics.Color.blue(pixel)-android.graphics.Color.blue(expected)).all { kotlin.math.abs(it)<70 })
                } finally { bitmap.recycle() }
            }
            var picture=output(task(SCREEN_OBSERVE,obj()))
            File(context.getExternalFilesDir(null),"vision-canvas.jpg").writeBytes(imageBytes(picture))
            File(context.getExternalFilesDir(null),"vision-canvas-state.json").writeText(picture.objectAt("state").toString()+"\n"+picture.objectAt("image").filterKeys { it!="data_base64" }.toString())
            context.sendBroadcast(Intent("ai.nexus.android.test.SCREEN_CHANGE").setPackage("ai.nexus.android.test"));delay(300)
            picture=output(task(SCREEN_ACT,act(picture,"tap",500,450)));color(picture,500,450,android.graphics.Color.GREEN)
            picture=output(task(SCREEN_ACT,act(picture,"long_press",500,900)));color(picture,500,900,android.graphics.Color.YELLOW)
            picture=output(task(SCREEN_ACT,act(picture,"swipe",650,1450,300,1450)));color(picture,500,1450,android.graphics.Color.BLUE)
            context.sendBroadcast(Intent("ai.nexus.android.test.SCREEN_CHANGE").setPackage("ai.nexus.android.test").putExtra("target",true));delay(300)
            val changed=task(SCREEN_ACT,act(picture,"tap",500,450))
            assertEquals("failed",changed.objectAt("task").text("status"));assertTrue(changed.toString().contains("目标区域已变化"))
            val cropped=output(task(SCREEN_OBSERVE,obj("region" to JsonArray(listOf(120,240,900,580).map(::JsonPrimitive)))))
            assertEquals(JsonArray(listOf(120,240,900,580).map(::JsonPrimitive)),cropped.objectAt("state").getValue("region"))
            picture=output(task(SCREEN_ACT,act(cropped,"tap",500,450)));color(picture,500,450,android.graphics.Color.GREEN)
            showChat("screen window changed")
            waitFor("NEXUS before stale screen action") { automation.rootInActiveWindow?.packageName?.toString()==context.packageName }
            assertEquals("failed",task(SCREEN_ACT,act(picture,"tap",500,450)).objectAt("task").text("status"))
            val selfScreen=output(task(SCREEN_OBSERVE,obj()))
            assertEquals("failed",task(SCREEN_ACT,act(selfScreen,"tap",500,450)).objectAt("task").text("status"))
            local.set("vision",false)
            waitFor("screen permission revoked") { !advertised().getValue("tools").toString().contains(SCREEN_ACT) }
            println("PHONE_SCREEN verified tap, long press, swipe, crop mapping, local refresh, stale target/window and self-protection")
            local.set("accessibility",false)
            waitFor("local accessibility disabled") { advertised().let { it.text("online")=="true" && !it.getValue("tools").toString().contains(UI_ACT) } }
            context.startActivity(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT))
            instrumentation.runOnMainSync { androidx.lifecycle.ViewModelProvider(main as MainActivity)[AppModel::class.java].page=2 }
            delay(1500)
            File(context.getExternalFilesDir(null),"phone-permissions.png").outputStream().use {
                check(automation.takeScreenshot().compress(android.graphics.Bitmap.CompressFormat.PNG,100,it))
            }
        } finally {
            context.startService(Intent(context,NodeService::class.java).setAction("STOP"))
            local.set("accessibility",false);local.set("files",false);local.set("vision",false)
            automation.executeShellCommand("settings delete secure enabled_accessibility_services").close()
            automation.executeShellCommand("settings put secure accessibility_enabled 0").close()
            instrumentation.runOnMainSync { main.finish() }
        }
    }
}
