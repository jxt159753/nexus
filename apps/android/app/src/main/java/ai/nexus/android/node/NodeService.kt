package ai.nexus.android.node

import android.app.*
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.*
import ai.nexus.android.MainActivity
import ai.nexus.android.security.Vault
import kotlinx.coroutines.*

class NodeService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var link: NodeLink? = null
    private var worker: Job? = null
    @Volatile private var state = "设备未启动"
    private val messenger = Messenger(Handler(Looper.getMainLooper()) { request ->
        if(request.what == 1) runCatching { request.replyTo?.send(Message.obtain(null,1).apply { data = Bundle().apply { putString("state",state); putBoolean("running",link != null) } }) }
        if(request.what == 2) stopExecution()
        true
    })
    override fun onBind(intent: Intent?) = messenger.binder
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if(intent?.action == "STOP") { stopExecution(); return START_NOT_STICKY }
        if(link != null) return START_NOT_STICKY
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel("node","设备执行",NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this,0,Intent(this,MainActivity::class.java),PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this,1,Intent(this,NodeService::class.java).setAction("STOP"),PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val notification = Notification.Builder(this,"node").setContentTitle("NEXUS 设备执行已开启")
            .setContentText("接收获准的设备任务；点击停止可关闭").setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentIntent(open).setOngoing(true).addAction(Notification.Action.Builder(null,"停止",stop).build()).build()
        if(Build.VERSION.SDK_INT >= 34) startForeground(21,notification,ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE) else startForeground(21,notification)
        try {
            val config = NodeConfig.from(requireNotNull(Vault(this,"node").read()) { "请先配置设备身份" })
            val runningLink = NodeLink(this,config) { state = it }
            link = runningLink
            worker = scope.launch {
                try { runningLink.run() }
                catch(cancel: CancellationException) { throw cancel }
                catch(failure: Exception) { state = "设备启动失败，请核对身份与回执；未执行新任务" }
                finally { withContext(NonCancellable + Dispatchers.Main) { if(link===runningLink) { link=null;if(state == "正在停止设备执行") state = "设备执行已停止"; stopForeground(STOP_FOREGROUND_REMOVE); stopSelf() } } }
            }
        } catch(failure: Exception) { state = "设备配置无效，请保存有效身份后启动"; stopForeground(STOP_FOREGROUND_REMOVE);stopSelf() }
        return START_NOT_STICKY
    }
    private fun stopExecution() { state = if(link==null) "设备执行已停止" else "正在停止设备执行"; link?.stop(); worker?.cancel(); stopSelf() }
    override fun onDestroy() { link?.stop(); scope.cancel(); super.onDestroy() }
}
