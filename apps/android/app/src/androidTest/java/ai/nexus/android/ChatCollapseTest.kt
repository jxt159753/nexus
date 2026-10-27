package ai.nexus.android

import android.content.Intent
import android.graphics.Bitmap
import android.view.accessibility.AccessibilityNodeInfo
import androidx.lifecycle.ViewModelProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import ai.nexus.android.client.AppModel
import ai.nexus.android.client.CoreClient
import ai.nexus.android.client.text
import ai.nexus.android.client.text
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class ChatCollapseTest {
    @Test fun completionCollapsesAndManualExpansionSurvivesRefresh() = runBlocking {
        val i=InstrumentationRegistry.getInstrumentation();val context=i.targetContext
        val automation=i.uiAutomation
        val fixture=wireJson.parseToJsonElement(File(context.filesDir,"android-fixture.json").readText()).jsonObject
        CoreClient(Vault(context,"owner")).apply { login(fixture.text("url"),fixture.text("owner_token"));http.dispatcher.cancelAll() }
        val activity=i.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        lateinit var model:AppModel
        fun nodes(node:AccessibilityNodeInfo?):List<AccessibilityNodeInfo> {
            if(node==null || !node.refresh())return emptyList()
            return listOf(node)+(0 until node.childCount).flatMap{nodes(node.getChild(it))}
        }
        fun all()=nodes(automation.rootInActiveWindow?.also{it.refresh()})
        fun contains(text:String)=all().any{it.text?.toString()?.contains(text)==true}
        suspend fun awaitText(text:String) { withTimeout(5000) { while(!contains(text))delay(100) } }
        fun click(text:String) {
            var node:AccessibilityNodeInfo?=all().firstOrNull{it.text?.toString()?.startsWith(text)==true} ?: error("Missing control: $text; visible: ${all().mapNotNull{it.text}.joinToString()}")
            while(node!=null && !node.isClickable)node=node.parent
            assertTrue(node?.performAction(AccessibilityNodeInfo.ACTION_CLICK)==true)
        }
        suspend fun show(phase:String,collapse:Boolean,revision:Int=1) {
            val permission=phase=="awaiting_input"
            i.runOnMainSync {
                model.history=obj("conversation" to obj("conversation_id" to str("collapse-fixture"),"title" to str("回复与处理过程")),"turns" to JsonArray(listOf(obj(
                    "turn_id" to str("collapse-turn"),"text" to str("整理资料并显示结论"),"status" to str(if(collapse) "completed" else "generating"),"revision" to JsonPrimitive(revision),
                    "presentation" to obj("phase" to str(phase),"label" to str(if(permission) "等待确认" else "部分操作未完成"),"collapse_process" to JsonPrimitive(collapse),"final_text" to str(if(collapse) "资料已整理，部分网页未能读取。" else "")),
                    "work_steps" to JsonArray(listOf(obj("step_id" to str("step"),"node_label" to str("折叠验证步骤"),"status" to str(if(permission) "permission" else "failed"),"proposal" to obj("intent" to str("web_fetch"),"reason" to str("确认后才能继续")))))))))
            }
            delay(600)
        }
        try {
            i.runOnMainSync { model=ViewModelProvider(activity)[AppModel::class.java] }
            withTimeout(10000) { while(model.busy)delay(100) }
            i.runOnMainSync { model.foreground(false);model.page=0;model.history=obj() }
            delay(500)
            assertTrue("Local fixture login must succeed",model.signedIn)
            show("responding",false)
            awaitText("查看处理过程")
            click("查看处理过程");awaitText("折叠验证步骤")
            show("completed",true)
            awaitText("资料已整理");assertFalse(contains("折叠验证步骤"));assertTrue(contains("部分操作未完成"))
            val image=requireNotNull(i.uiAutomation.takeScreenshot())
            File(context.getExternalFilesDir(null),"chat-collapsed.png").outputStream().use{assertTrue(image.compress(Bitmap.CompressFormat.PNG,100,it))}
            click("查看处理过程");awaitText("折叠验证步骤")
            show("completed",true,2);awaitText("收起过程");assertTrue(contains("折叠验证步骤"))
            click("收起过程");delay(300)
            show("awaiting_input",false,3);awaitText("同意本步骤")
        } catch(error:Throwable) {
            i.uiAutomation.takeScreenshot()?.let { image -> File(context.getExternalFilesDir(null),"chat-collapse-failure.png").outputStream().use { image.compress(Bitmap.CompressFormat.PNG,100,it) } }
            throw error
        } finally { i.runOnMainSync { activity.finish() } }
    }

}
