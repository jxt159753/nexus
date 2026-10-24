package ai.nexus.android

import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Rect
import android.view.View
import android.view.ViewGroup
import android.view.accessibility.AccessibilityNodeInfo
import android.webkit.WebView
import androidx.lifecycle.ViewModelProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import ai.nexus.android.client.*
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class AdaptiveUiTest {
    @Test fun sharedSceneAndAdaptiveChat()=runBlocking {
        val i=InstrumentationRegistry.getInstrumentation();val context=i.targetContext
        val fixtureFile=File(context.filesDir,"android-fixture.json");assumeTrue(fixtureFile.exists())
        val fixture=wireJson.parseToJsonElement(fixtureFile.readText()).jsonObject
        CoreClient(Vault(context,"owner")).apply { if(restore()) checkSession() else login(fixture.text("url"),fixture.text("owner_token"));http.dispatcher.cancelAll() }
        var activity=i.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        lateinit var model:AppModel
        fun web(view:View):WebView? { if(view is WebView)return view;if(view is ViewGroup)for(n in 0 until view.childCount)web(view.getChildAt(n))?.let{return it};return null }
        fun nodes(node:AccessibilityNodeInfo?):List<AccessibilityNodeInfo> = if(node==null)emptyList() else listOf(node)+(0 until node.childCount).flatMap{nodes(node.getChild(it))}
        fun all()=nodes(i.uiAutomation.rootInActiveWindow)
        fun click(text:String) { val node=all().filter{it.text?.toString()==text}.maxByOrNull{Rect().also(it::getBoundsInScreen).centerX()} ?: error("Missing control: $text");var current:AccessibilityNodeInfo?=node;while(current!=null && !current.isClickable)current=current.parent;assertTrue("Click $text",current?.performAction(AccessibilityNodeInfo.ACTION_CLICK)==true) }
        suspend fun script(code:String):String { val latch=CountDownLatch(1);var result="";i.runOnMainSync{val w=web(activity.window.decorView);if(w==null)latch.countDown() else w.evaluateJavascript(code){result=it;latch.countDown()}};assertTrue(latch.await(4,TimeUnit.SECONDS));return result }
        val name=InstrumentationRegistry.getArguments().getString("layoutName") ?: "layout"
        fun capture(suffix:String) { val image=requireNotNull(i.uiAutomation.takeScreenshot());File(context.getExternalFilesDir(null),"$name-$suffix.png").outputStream().use{assertTrue(image.compress(Bitmap.CompressFormat.PNG,100,it))} }
        try {
            i.runOnMainSync { activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE);model=ViewModelProvider(activity)[AppModel::class.java] }
            repeat(40){if(model.busy)delay(100)}
            i.runOnMainSync {
                model.foreground(false);model.page=0
                model.nodes=listOf(obj("node_id" to str("computer"),"label" to str("书房电脑"),"online" to JsonPrimitive(true),"tools" to JsonArray(listOf(str("system.info@1")))),obj("node_id" to str("phone"),"label" to str("随身手机"),"online" to JsonPrimitive(false)))
                model.tasks=listOf(obj("task_id" to str("layout-task"),"node_id" to str("computer"),"status" to str("running"),"created_at" to str("2026-10-24T00:00:00Z")))
                model.history=obj();model.editDraft("平板、横屏和分屏中保留这份草稿")
            }
            var ready=false
            repeat(60){if(!ready){ready=script("window.sceneReady === true && document.querySelectorAll('canvas').length > 0")=="true";if(!ready)delay(250)}}
            assertTrue("Bundled WebGL scene must render",ready)
            capture("loaded")
            assertEquals(script("JSON.stringify({bridge:typeof window.Android,height:document.getElementById('visual-canvas').clientHeight,width:innerWidth,url:location.href})"),"true",script("!window.Android && document.getElementById('visual-canvas').clientHeight > 80"))
            assertEquals("true",script("(()=>{window.draws=0;for(const name of ['drawArrays','drawElements']){const fn=WebGL2RenderingContext.prototype[name];WebGL2RenderingContext.prototype[name]=function(...args){window.draws++;return fn.apply(this,args)}}window.sceneVisible(true);return true})()"))
            delay(800);val running=script("window.draws").toInt();assertTrue("scene renders frames",running>0)
            script("window.sceneVisible(false)");delay(100);val stopped=script("window.draws");delay(350);assertEquals("hidden scene must stop drawing",stopped,script("window.draws"));script("window.sceneVisible(true)")
            capture("scene")
            i.runOnMainSync { model.history=obj("conversation" to obj("conversation_id" to str("ui-fixture"),"title" to str("一起完成今天的工作")),"turns" to JsonArray(listOf(obj("turn_id" to str("turn-fixture"),"text" to str("帮我整理资料，并把结果给我。"),"status" to str("completed"),"response_text" to str("## 已整理好\n\n这里是你的工作结果。\n\n- 重点清楚，过程可以展开\n- 文件直接预览或打开\n\n```text\n长内容也应该保持清晰可读。\n```"),"work_steps" to JsonArray(listOf(obj("status" to str("succeeded"),"node_label" to str("书房电脑"),"proposal" to obj("intent" to str("read_file"))))))))) }
            delay(1200);capture("chat")
            assertTrue(all().any{it.text?.toString()=="发送"})
            assertFalse(all().any{it.text?.toString()?.contains("succeeded")==true})
            assertEquals("平板、横屏和分屏中保留这份草稿",model.draft)
            if(context.resources.configuration.screenWidthDp<900) { click("动态");delay(800);assertTrue(script("window.sceneReady === true")=="true");click("对话");delay(300) }
            val editor=all().firstOrNull{it.isEditable};assertNotNull(editor);editor!!.performAction(AccessibilityNodeInfo.ACTION_CLICK);delay(700)
            val send=all().first{it.text?.toString()=="发送"};val rect=Rect();send.getBoundsInScreen(rect)
            assertTrue("Send remains within window",rect.top>=0 && rect.bottom<=context.resources.displayMetrics.heightPixels)
            capture("keyboard")
            i.uiAutomation.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_BACK);delay(300)
            i.runOnMainSync{model.page=2};delay(500)
            assertFalse(all().any{it.text?.toString()?.contains("文件传输")==true});capture("devices")
            i.uiAutomation.setRotation(android.app.UiAutomation.ROTATION_FREEZE_90);delay(1500)
            i.runOnMainSync {
                activity=ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED).filterIsInstance<MainActivity>().single()
                model=ViewModelProvider(activity)[AppModel::class.java]
                activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE)
                assertEquals("平板、横屏和分屏中保留这份草稿",model.draft)
            }
            delay(500);capture("rotated")
        } finally { i.runOnMainSync{activity.finish()};i.uiAutomation.setRotation(android.app.UiAutomation.ROTATION_UNFREEZE) }
    }
}
