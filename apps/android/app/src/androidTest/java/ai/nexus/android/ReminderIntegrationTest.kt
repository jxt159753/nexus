package ai.nexus.android

import android.app.NotificationManager
import android.content.Intent
import android.os.Build
import ai.nexus.android.client.uid
import ai.nexus.android.reminders.ReminderService
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/** Explicit emulator-only OS test. Uses loopback HTTP, never a real account. */
@RunWith(AndroidJUnit4::class)
class ReminderIntegrationTest {
    @Test fun backgroundLockedDeliveryRetryAndRevocation(): Unit = runBlocking {
        check(Build.FINGERPRINT.contains("generic") || Build.MODEL.contains("sdk")) { "Dedicated emulator required" }
        val test=InstrumentationRegistry.getInstrumentation()
        val context=test.targetContext
        val vault=Vault(context,"reminders")
        check(vault.read()==null) { "Reminder credentials must be empty in test profile" }
        fun shell(command:String)=test.uiAutomation.executeShellCommand(command).use { android.os.ParcelFileDescriptor.AutoCloseInputStream(it).bufferedReader().readText() }
        shell("pm grant ${context.packageName} android.permission.POST_NOTIFICATIONS")
        val manager=context.getSystemService(NotificationManager::class.java)
        val socket=ServerSocket(0,10,InetAddress.getByName("127.0.0.1"))
        val ready=AtomicBoolean(false);val revoked=AtomicBoolean(false);val acknowledgments=AtomicInteger()
        val identifier=uid()
        val server=Thread {
            while(!socket.isClosed) runCatching {
                socket.accept().use { connection ->
                    val reader=connection.getInputStream().bufferedReader()
                    val request=reader.readLine() ?: return@use
                    val headers=mutableListOf<String>()
                    while(true) { val line=reader.readLine() ?: break;if(line.isEmpty()) break;headers.add(line) }
                    check(headers.any { it.equals("Authorization: Bearer "+"t".repeat(43),true) })
                    val code=when { revoked.get()->401;request.startsWith("POST")->if(acknowledgments.incrementAndGet()==1) 503 else 200;else->200 }
                    val content=if(request.startsWith("GET") && ready.get() && acknowledgments.get()<2)
                        """{"reminders":[{"reminder_id":"$identifier","conversation_id":"00000000-0000-4000-8000-000000000001","title":"锁屏提醒验证","body":"无 Google 推送 · 后台通知实际提交"}]}"""
                        else """{"reminders":[]}"""
                    val bytes=content.toByteArray()
                    connection.getOutputStream().apply { write("HTTP/1.1 $code Test\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n".toByteArray());write(bytes);flush() }
                }
            }
        }.apply { isDaemon=true;start() }
        val activity=test.startActivitySync(Intent(context,MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        suspend fun until(condition:()->Boolean)=withTimeout(45000) { while(!condition()) delay(100) }
        val priorWhitelist=shell("dumpsys deviceidle whitelist").contains(context.packageName)
        try {
            vault.write(obj("url" to str("http://127.0.0.1:${socket.localPort}"),"token" to str("t".repeat(43)),"client_id" to str(uid())))
            context.getSharedPreferences("reminder-status",0).edit().putBoolean("enabled",true).commit()
            context.startForegroundService(Intent(context,ReminderService::class.java))
            until { manager.activeNotifications.any { it.id==22 } }
            shell("input keyevent KEYCODE_HOME")
            shell("dumpsys deviceidle whitelist +${context.packageName}")
            shell("input keyevent KEYCODE_SLEEP")
            shell("dumpsys deviceidle force-idle")
            ready.set(true)
            until { acknowledgments.get()>=1 }
            val first=manager.activeNotifications.single { it.tag==identifier }
            assertEquals("锁屏提醒验证",first.notification.extras.getString("android.title"))
            until { acknowledgments.get()>=2 }
            assertEquals(first.postTime,manager.activeNotifications.single { it.tag==identifier }.postTime)
            revoked.set(true)
            until { !context.getSharedPreferences("reminder-status",0).getBoolean("enabled",true) }
            until { manager.activeNotifications.none { it.id==22 } }
            assertTrue(context.getSharedPreferences("reminder-status",0).getString("state","")!!.contains("授权已失效"))
        } finally {
            context.stopService(Intent(context,ReminderService::class.java))
            manager.cancel(identifier,1);vault.clear()
            context.getSharedPreferences("reminder-status",0).edit().clear().commit()
            context.getSharedPreferences("reminder-posted",0).edit().clear().commit()
            shell("dumpsys deviceidle unforce")
            if(!priorWhitelist) shell("dumpsys deviceidle whitelist -${context.packageName}")
            shell("input keyevent KEYCODE_WAKEUP")
            test.runOnMainSync { activity.finish() }
            socket.close();server.join(1000)
        }
    }
}
