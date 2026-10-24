package ai.nexus.android

import android.content.Intent
import android.graphics.Bitmap
import androidx.lifecycle.ViewModelProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import ai.nexus.android.client.*
import ai.nexus.android.security.Vault
import ai.nexus.protocol.wireJson
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class UiSnapshotTest {
    @Test fun renderRealCoreHistoryAndDeviceSettings() = runBlocking {
        val instrumentation=InstrumentationRegistry.getInstrumentation()
        val context=instrumentation.targetContext
        assumeTrue(File(context.filesDir,"android-fixture.json").exists())
        val client=CoreClient(Vault(context,"owner"));assertTrue(client.restore());client.checkSession()
        val conversation=client.request("GET","/api/v1/chat/conversations").objects("conversations").first()
        val history=client.request("GET","/api/v1/chat/conversations/${conversation.text("conversation_id")}/turns")
        val activity=instrumentation.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        lateinit var model: AppModel
        instrumentation.runOnMainSync {
            activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE)
            model=ViewModelProvider(activity)[AppModel::class.java]
        }
        delay(1500)
        instrumentation.runOnMainSync { model.history=history;model.page=0 }
        for(page in listOf(0,2)) {
            instrumentation.runOnMainSync { model.page=page }
            delay(1000);instrumentation.waitForIdleSync()
            val bitmap=requireNotNull(instrumentation.uiAutomation.takeScreenshot())
            File(context.getExternalFilesDir(null),"android-page-$page.png").outputStream().use { assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG,100,it)) }
        }
        instrumentation.runOnMainSync { activity.finish() }
    }
}
