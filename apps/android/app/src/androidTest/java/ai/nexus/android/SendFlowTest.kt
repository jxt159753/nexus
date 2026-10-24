package ai.nexus.android

import android.content.Intent
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
class SendFlowTest {
    @Test fun firstMessageAndFollowupOfferConsentWithoutHiddenCheckboxOrNewConversation() = runBlocking {
        val instrument=InstrumentationRegistry.getInstrumentation()
        val context=instrument.targetContext
        val fixtureFile=File(context.filesDir,"android-fixture.json")
        assumeTrue(fixtureFile.exists())
        val fixture=wireJson.parseToJsonElement(fixtureFile.readText()).jsonObject
        val client=CoreClient(Vault(context,"owner"))
        client.login(fixture.text("url"),fixture.text("owner_token"))
        val before=client.request("GET","/api/v1/chat/conversations").objects("conversations").size
        val activity=instrument.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        lateinit var model: AppModel
        instrument.runOnMainSync { model=ViewModelProvider(activity)[AppModel::class.java] }
        suspend fun waitFor(check: () -> Boolean)=withTimeout(25000) { while(!withContext(Dispatchers.Main){check()}) delay(100) }
        fun find(node: AccessibilityNodeInfo?, text: String): AccessibilityNodeInfo? {
            if(node==null)return null
            if(node.text?.toString()==text)return node
            for(i in 0 until node.childCount) find(node.getChild(i),text)?.let{return it}
            return null
        }
        suspend fun click(text: String) {
            withTimeout(5000) {
                while(true) {
                    var node=find(instrument.uiAutomation.rootInActiveWindow,text)
                    while(node!=null && !node.isClickable) node=node.parent
                    if(node!=null) { assertTrue("$text must be enabled",node.isEnabled);assertTrue(node.performAction(AccessibilityNodeInfo.ACTION_CLICK));break }
                    delay(100)
                }
            }
        }
        try {
            waitFor { model.signedIn && !model.busy }
            instrument.runOnMainSync { model.history=JsonObject(emptyMap());model.page=0;model.allowModel=false;model.allowTools=true;model.editDraft("告诉我设备情况") }
            waitFor { model.sendBlockedReason.isEmpty() }
            click("发送");click("返回")
            assertEquals(before,client.request("GET","/api/v1/chat/conversations").objects("conversations").size)
            click("发送");click("同意并发送")
            waitFor { !model.busy && model.history.objects("turns").any{it.text("status")=="completed"} }
            val id=withContext(Dispatchers.Main){model.conversationId}
            assertTrue(id.isNotEmpty());assertFalse(withContext(Dispatchers.Main){model.allowModel})
            instrument.runOnMainSync { model.editDraft("再告诉我一次") }
            waitFor { model.sendBlockedReason.isEmpty() }
            click("发送");click("同意并发送")
            waitFor { !model.busy && model.history.objects("turns").count{it.text("status")=="completed"}==2 }
            assertEquals(id,withContext(Dispatchers.Main){model.conversationId})
            assertEquals(before+1,client.request("GET","/api/v1/chat/conversations").objects("conversations").size)
            assertNull(withContext(Dispatchers.Main){model.pending})
            assertTrue(withContext(Dispatchers.Main){model.history.objects("turns").all{it.objectAt("presentation").text("final_text").contains("本地测试替身")}})
        } finally { instrument.runOnMainSync { activity.finish() };client.http.dispatcher.cancelAll() }
    }
}
