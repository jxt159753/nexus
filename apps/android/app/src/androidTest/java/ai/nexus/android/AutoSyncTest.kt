package ai.nexus.android

import android.content.Intent
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
class AutoSyncTest {
    @Test fun externalChangesAppearWithoutRefreshAndBackgroundStopsPolling() = runBlocking {
        val instrument = InstrumentationRegistry.getInstrumentation()
        val context = instrument.targetContext
        val file = File(context.filesDir,"android-fixture.json")
        assumeTrue(file.exists())
        val fixture = wireJson.parseToJsonElement(file.readText()).jsonObject
        val client = CoreClient(Vault(context,"owner"))
        client.login(fixture.text("url"),fixture.text("owner_token"))
        val activity = instrument.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        lateinit var model: AppModel
        instrument.runOnMainSync { model = ViewModelProvider(activity as MainActivity)[AppModel::class.java] }
        suspend fun waitFor(check: () -> Boolean) = withTimeout(20000) {
            while(!withContext(Dispatchers.Main) { check() }) delay(200)
        }
        try {
            waitFor { model.signedIn && model.syncState=="已同步" }
            val id = client.request("POST","/api/v1/chat/conversations",obj("request_id" to str(uid()))).objectAt("conversation").text("conversation_id")
            client.request("PATCH","/api/v1/chat/conversations/$id",obj("title" to str("自动同步测试")))
            waitFor { model.conversations.any { it.text("conversation_id")==id && it.text("title")=="自动同步测试" } }
            client.request("PATCH","/api/v1/nodes/${fixture.text("node_id")}/name",obj("name" to str("测试手机")))
            waitFor { model.nodes.any { it.text("label")=="测试手机" } }
            instrument.runOnMainSync { model.page=1 }
            val task=client.request("POST","/api/v1/tasks",obj("node_id" to str(fixture.text("node_id")),"request" to obj("request_id" to str(uid()),"intent" to str("android_device_status"),"arguments" to obj()))).objectAt("task")
            waitFor { model.tasks.any { it.text("task_id")==task.text("task_id") } }
            instrument.runOnMainSync { model.foreground(false) }
            client.request("PATCH","/api/v1/nodes/${fixture.text("node_id")}/name",obj("name" to str("后台更新手机")))
            delay(3500)
            assertFalse(withContext(Dispatchers.Main) { model.nodes.any{it.text("label")=="后台更新手机"} })
            instrument.runOnMainSync { model.foreground(true) }
            waitFor { model.nodes.any { it.text("label")=="后台更新手机" } }
            assertNull(withContext(Dispatchers.Main) { model.pending })
        } finally { instrument.runOnMainSync { activity.finish() }; client.http.dispatcher.cancelAll() }
    }
}
