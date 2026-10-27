package ai.nexus.android

import android.content.Intent
import android.graphics.Bitmap
import android.view.accessibility.AccessibilityNodeInfo
import androidx.lifecycle.ViewModelProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
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

@RunWith(AndroidJUnit4::class)
class AssistantSettingsUiTest {
    @Test fun savedStylePreviewAndCrossClientConflict() = runBlocking {
        val i=InstrumentationRegistry.getInstrumentation(); val context=i.targetContext
        val file=File(context.filesDir,"personality-fixture.json"); assumeTrue(file.exists())
        val fixture=wireJson.parseToJsonElement(file.readText()).jsonObject
        val client=CoreClient(Vault(context,"owner"))
        client.login(fixture.text("url"),fixture.text("owner_token"))
        val activity=i.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        lateinit var model:AppModel
        fun nodes(node:AccessibilityNodeInfo?):List<AccessibilityNodeInfo> = if(node==null) emptyList() else listOf(node)+(0 until node.childCount).flatMap{nodes(node.getChild(it))}
        fun all():List<AccessibilityNodeInfo> {
            if(android.os.Build.VERSION.SDK_INT>=33) i.uiAutomation.clearCache()
            return nodes(i.uiAutomation.rootInActiveWindow)
        }
        suspend fun click(text:String) {
            i.waitForIdleSync()
            i.uiAutomation.waitForIdle(150,3000)
            repeat(7) { attempt ->
                val found=all().firstOrNull{it.text?.toString()==text}
                if(found!=null) {
                    var target:AccessibilityNodeInfo?=found
                    while(target!=null && !target.isClickable) target=target.parent
                    assertTrue("Click $text",target?.performAction(AccessibilityNodeInfo.ACTION_CLICK)==true)
                    delay(200); return
                }
                all().firstOrNull{it.isScrollable}?.performAction(if(attempt==0) AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD else AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
                delay(200)
            }
            i.uiAutomation.takeScreenshot()?.let { image -> File(context.getExternalFilesDir(null),"settings-failure.png").outputStream().use { image.compress(Bitmap.CompressFormat.PNG,100,it) } }
            error("Missing control: $text; seen: "+all().mapNotNull{it.text?.toString()}.joinToString(" / "))
        }
        suspend fun idle() { withTimeout(10000) { while(model.busy) delay(50) };delay(200) }
        try {
            i.runOnMainSync { model=ViewModelProvider(activity)[AppModel::class.java] }
            withTimeout(10000) { while(!model.signedIn) delay(50) };idle()
            i.runOnMainSync { model.foreground(false);model.page=3 }
            withTimeout(10000) { while(model.assistantSettings==null) delay(50) };idle()
            click("NEXUS · 沉稳机智");click("温暖伙伴")
            click("适量");click("丰富")
            click("保存表达偏好");idle()
            var current=client.request("GET","/api/v1/assistant/preferences").objectAt("preferences")
            assertEquals("companion",current.text("personality"));assertEquals("expressive",current.text("emoji"))
            click("读取最新设置");idle()
            assertEquals("companion",model.assistantSettings!!.objectAt("preferences").text("personality"))
            // External client wins; stale UI must not silently overwrite it.
            client.request("PUT","/api/v1/assistant/preferences",obj("personality" to str("advisor"),"emoji" to str("off"),"expected_revision" to current.getValue("revision")))
            i.runOnMainSync { model.saveAssistantSettings("explorer","natural",current.getValue("revision")) };idle()
            assertTrue(model.assistantSettingsStale)
            click("读取最新设置");idle()
            current=model.assistantSettings!!.objectAt("preferences")
            assertEquals("advisor",current.text("personality"));assertEquals("off",current.text("emoji"))
            // Return to the top and capture the real Compose preview with Emoji.
            repeat(5) { all().firstOrNull{it.isScrollable}?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD);delay(100) }
            click("严谨顾问");click("温暖伙伴");click("关闭");click("适量")
            assertTrue(all().any{it.text?.toString()?.contains("🌿")==true})
            val bitmap=requireNotNull(i.uiAutomation.takeScreenshot())
            File(context.getExternalFilesDir(null),"personality-settings.png").outputStream().use { assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG,100,it)) }
        } finally {
            client.http.dispatcher.cancelAll()
            i.runOnMainSync { activity.finish() }
        }
    }
}
