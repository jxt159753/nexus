package ai.nexus.android

import android.content.Intent
import android.graphics.Bitmap
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import ai.nexus.android.ui.Markdown
import kotlinx.coroutines.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class MarkdownUiTest {
    @Test fun renderSharedMarkdownAndBlockActiveContent()=runBlocking {
        val i=InstrumentationRegistry.getInstrumentation();val context=i.targetContext
        val activity=i.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        val text="# 文件已送达\n\n**demo.txt** · 校验通过\n\n- 电脑读取原始文件\n- 手机收件箱已保存\n\n```text\nSHA-256: 597ef222…0f52f3\n```\n\n| 文件 | 状态 |\n| --- | --- |\n| demo.txt | 已送达 |\n\n[查看说明](https://example.com)\n\n<script>window.pwned=1</script><img src=https://example.com/leak onerror=alert(1)>"
        i.runOnMainSync { activity.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE);activity.setContent{MaterialTheme{Column(Modifier.padding(16.dp)){Markdown(text)}}} }
        fun web(view:View):WebView? { if(view is WebView)return view;if(view is ViewGroup)for(n in 0 until view.childCount)web(view.getChildAt(n))?.let{return it};return null }
        try {
            var result="";var ready=false
            repeat(60) {
                if(!ready) {
                    val latch=CountDownLatch(1)
                    i.runOnMainSync { val w=web(activity.window.decorView);if(w==null)latch.countDown() else w.evaluateJavascript("JSON.stringify({h:document.querySelectorAll('#content h1').length,b:document.querySelectorAll('#content strong').length,li:document.querySelectorAll('#content li').length,t:document.querySelectorAll('#content table').length,code:document.querySelectorAll('#content pre code').length,bad:document.querySelectorAll('#content img,#content script').length,p:window.pwned||0})"){result=it;latch.countDown()} }
                    latch.await(2,TimeUnit.SECONDS);ready=result.contains("\\\"h\\\":1");if(!ready)delay(100)
                }
            }
            assertTrue(result,ready);assertTrue(result,result.contains("\\\"t\\\":1"));assertTrue(result,result.contains("\\\"li\\\":2"));assertTrue(result,result.contains("\\\"bad\\\":0"));assertTrue(result,result.contains("\\\"p\\\":0"))
            delay(1500);i.waitForIdleSync()
            val bitmap=requireNotNull(i.uiAutomation.takeScreenshot())
            File(context.getExternalFilesDir(null),"markdown-chat.png").outputStream().use{assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG,100,it))}
        } finally { i.runOnMainSync {activity.finish()} }
    }
}
