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
class SceneSyncTest {
    @Test fun latestTasksGlobalActivityAndIndependentDevicePolling() = runBlocking {
        val i = InstrumentationRegistry.getInstrumentation()
        val context = i.targetContext
        val file = File(context.filesDir,"android-fixture.json")
        assumeTrue(file.exists())
        val fixture = wireJson.parseToJsonElement(file.readText()).jsonObject
        val desktop = CoreClient(Vault(context,"owner"))
        desktop.login(fixture.text("url"),fixture.text("owner_token"))
        val activity = i.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        lateinit var model: AppModel
        i.runOnMainSync { model = ViewModelProvider(activity)[AppModel::class.java] }
        suspend fun waitFor(check: () -> Boolean) = withTimeout(18000) { while(!withContext(Dispatchers.Main) { check() }) delay(100) }
        try {
            waitFor { model.activityFresh && model.nodesFresh && !model.busy }
            val latest = desktop.request("GET","/api/v1/client/snapshot")
            assertEquals(latest.objects("tasks").map { it.text("task_id") }, model.tasks.take(20).map { it.text("task_id") })
            // Seeded with >100 historical tasks: a newly created task must still appear.
            val task = desktop.request("POST","/api/v1/tasks",obj("node_id" to str(fixture.text("node_id")),"request" to obj("request_id" to str(uid()),"intent" to str("android_device_status"),"arguments" to obj()))).objectAt("task")
            waitFor { model.sceneTasks.any { it.text("task_id") == task.text("task_id") } }
            assertEquals("android_device_status",model.sceneTasks.first { it.text("task_id")==task.text("task_id") }.objectAt("request").text("intent"))
            i.runOnMainSync { model.taskPage(true) }
            waitFor { !model.busy && model.tasks.size > 20 }
            val loadedIds = model.tasks.map { it.text("task_id") }.toSet()
            delay(2500)
            assertTrue(model.tasks.map { it.text("task_id") }.containsAll(loadedIds))

            val conversation = desktop.request("POST","/api/v1/chat/conversations",obj("request_id" to str(uid()))).objectAt("conversation")
            val turn = desktop.request("POST","/api/v1/chat/conversations/${conversation.text("conversation_id")}/turns",obj("request_id" to str(uid()),"expected_revision" to conversation.getValue("revision"),"text" to str("等待"),"allow_model" to JsonPrimitive(true),"client_node_id" to str(fixture.text("node_id")))).objectAt("turn")
            waitFor { model.activity?.objects("turns")?.any { it.text("turn_id")==turn.text("turn_id") } == true }
            assertEquals("",model.activeTurn)
            assertEquals(desktop.request("GET","/api/v1/client/snapshot").objectAt("activity"),model.activity)
            // A slow user action/history request must not freeze device observations.
            i.runOnMainSync { model.action { delay(6000) } }
            desktop.request("PATCH","/api/v1/nodes/${fixture.text("node_id")}/name",obj("name" to str("同步验证手机")))
            withTimeout(5000) { waitFor { model.nodes.any { it.text("label")=="同步验证手机" } } }
            assertTrue(model.busy)
            desktop.request("POST","/api/v1/control/pause",obj("request_id" to str(uid())))
            waitFor { model.control.text("paused")=="true" }
            i.runOnMainSync { model.foreground(false) }
            desktop.request("PATCH","/api/v1/nodes/${fixture.text("node_id")}/name",obj("name" to str("恢复后手机")))
            desktop.request("POST","/api/v1/tasks/${task.text("task_id")}/cancel",obj())
            delay(2300)
            assertFalse(model.nodes.any { it.text("label")=="恢复后手机" })
            assertFalse(model.nodesFresh)
            i.runOnMainSync { model.foreground(true) }
            waitFor { model.nodesFresh && model.nodes.any { it.text("label")=="恢复后手机" } && model.tasks.any { it.text("task_id")==task.text("task_id") && it.text("status")=="cancelled" } }
            desktop.request("POST","/api/v1/chat/turns/${turn.text("turn_id")}/stop",obj())
            waitFor { model.activity?.objects("turns")?.none { it.text("turn_id")==turn.text("turn_id") } == true }
        } finally {
            desktop.request("POST","/api/v1/control/resume",obj("request_id" to str(uid())))
            i.runOnMainSync { activity.finish() }
            desktop.http.dispatcher.cancelAll()
        }
    }
}
