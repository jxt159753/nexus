package ai.nexus.android.reminders

import android.app.*
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.PowerManager
import ai.nexus.android.MainActivity
import ai.nexus.android.client.coreUrl
import ai.nexus.android.client.objects
import ai.nexus.android.client.text
import ai.nexus.android.security.Vault
import ai.nexus.protocol.wireJson
import kotlinx.coroutines.*
import kotlinx.serialization.json.jsonObject
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/** User-enabled delivery only; no owner session, model or device execution rights. */
class ReminderService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val http = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .connectTimeout(10,TimeUnit.SECONDS).callTimeout(15,TimeUnit.SECONDS).build()
    private var worker: Job? = null
    private var wake: PowerManager.WakeLock? = null
    override fun onBind(intent: Intent?) = null
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if(intent?.action == "STOP") {
            getSharedPreferences("reminder-status",0).edit().putBoolean("enabled",false).apply()
            stopSelf(); return START_NOT_STICKY
        }
        if(worker != null) return START_STICKY
        if(!getSharedPreferences("reminder-status",0).getBoolean("enabled",false)) { stopSelf(); return START_NOT_STICKY }
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel("reminder-service","后台提醒连接",NotificationManager.IMPORTANCE_LOW))
        manager.createNotificationChannel(NotificationChannel("reminders","NEXUS 提醒",NotificationManager.IMPORTANCE_HIGH))
        val open = PendingIntent.getActivity(this,0,Intent(this,MainActivity::class.java).putExtra("open_reminders",true),PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this,0,Intent(this,ReminderService::class.java).setAction("STOP"),PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val connection = Notification.Builder(this,"reminder-service").setContentTitle("NEXUS 后台提醒已开启")
            .setContentText("锁屏或离开 App 后继续接收；点击停止可关闭")
            .setSmallIcon(android.R.drawable.ic_dialog_info).setContentIntent(open).setOngoing(true)
            .addAction(Notification.Action.Builder(null,"停止",stop).build()).build()
        if(Build.VERSION.SDK_INT>=34) startForeground(22,connection,ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE) else startForeground(22,connection)
        worker=scope.launch {
            var delayMs=5000L
            while(isActive) {
                try {
                    // The user-visible non-GMS connection must remain runnable
                    // while locked. Released on stop, bounded and renewed here.
                    if(wake?.isHeld != true) {
                        wake=getSystemService(PowerManager::class.java).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"NEXUS:reminders").apply { acquire(10*60*1000L) }
                    }
                    val settings=requireNotNull(Vault(this@ReminderService,"reminders").read())
                    val base=coreUrl(settings.text("url"))
                    val token=settings.text("token")
                    val request=Request.Builder().url(base.resolve("/api/v1/reminders/delivery")!!).header("Authorization","Bearer $token").build()
                    http.newCall(request).execute().use { response ->
                        if(response.code in setOf(401,403)) {
                            getSharedPreferences("reminder-status",0).edit().putBoolean("enabled",false).putString("state","提醒授权已失效，请重新开启").apply()
                            stopSelf(); return@launch
                        }
                        check(response.isSuccessful)
                        val source=requireNotNull(response.body).source()
                        require(!source.request(256*1024L+1))
                        val items=wireJson.parseToJsonElement(source.readUtf8()).jsonObject.objects("reminders")
                        var blocked=false
                        for(item in items) {
                            ensureActive()
                            if(!manager.areNotificationsEnabled() || manager.getNotificationChannel("reminders").importance == NotificationManager.IMPORTANCE_NONE) { blocked=true; getSharedPreferences("reminder-status",0).edit().putString("state","系统通知被关闭，提醒等待投递").apply(); break }
                            val id=item.text("reminder_id")
                            val detail=Intent(this@ReminderService,MainActivity::class.java).setAction("reminder.$id")
                                .putExtra("reminder_id",id).putExtra("conversation_id",item.text("conversation_id")).putExtra("open_reminders",true)
                            val click=PendingIntent.getActivity(this@ReminderService,0,detail,PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
                            val notification=Notification.Builder(this@ReminderService,"reminders").setContentTitle(item.text("title"))
                                .setContentText(item.text("body")).setStyle(Notification.BigTextStyle().bigText(item.text("body")))
                                .setSmallIcon(android.R.drawable.ic_dialog_info).setContentIntent(click).setAutoCancel(true)
                                .setOnlyAlertOnce(true).setVisibility(Notification.VISIBILITY_PRIVATE).build()
                            val posted=getSharedPreferences("reminder-posted",0)
                            val seen=posted.getString("ids","")!!.split(',').filter{it.isNotEmpty()}.toMutableList()
                            if(id !in seen) {
                                manager.notify(id,1,notification)
                                seen.add(id)
                                check(posted.edit().putString("ids",seen.takeLast(256).joinToString(",")).commit())
                            }
                            // Stable notification tags replace an unacknowledged delivery;
                            // network loss never creates another alert identity.
                            http.newCall(Request.Builder().url(base.resolve("/api/v1/reminders/delivery/$id")!!)
                                .header("Authorization","Bearer $token").post(ByteArray(0).toRequestBody()).build()).execute().use { check(it.isSuccessful) }
                        }
                        if(!blocked) getSharedPreferences("reminder-status",0).edit().putString("state","提醒连接正常").apply()
                    }
                    delayMs=5000L
                } catch(cancelled:CancellationException) { throw cancelled }
                catch(error:Exception) {
                    getSharedPreferences("reminder-status",0).edit().putString("state","连接中断，恢复后补收提醒").apply()
                    delayMs=(delayMs*2).coerceAtMost(60000L)
                }
                delay(delayMs)
            }
        }
        return START_STICKY
    }
    override fun onDestroy() { scope.cancel();http.dispatcher.cancelAll();wake?.let { if(it.isHeld) it.release() };wake=null;super.onDestroy() }
}
