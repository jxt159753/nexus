package ai.nexus.android

import android.Manifest
import android.content.*
import android.content.pm.PackageManager
import android.os.*
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.lifecycle.ViewModelProvider
import androidx.compose.runtime.*
import ai.nexus.android.client.AppModel
import ai.nexus.android.node.NodeService
import ai.nexus.android.ui.NexusApp

class MainActivity : ComponentActivity() {
    private lateinit var model: AppModel
    private var service: Messenger? = null
    private var bound = false
    private var nodeState by mutableStateOf("设备未启动")
    private var nodeRunning by mutableStateOf(false)
    private val handler = Handler(Looper.getMainLooper())
    private val reply = Messenger(Handler(Looper.getMainLooper()) {
        nodeState = it.data.getString("state") ?: "设备未启动"
        nodeRunning = it.data.getBoolean("running"); true
    })
    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) { service = Messenger(binder) }
        override fun onServiceDisconnected(name: ComponentName?) { service = null; nodeRunning = false; nodeState = "设备进程已停止，重新启动后将先核对回执" }
    }
    private val check = object : Runnable {
        override fun run() { runCatching { service?.send(Message.obtain(null,1).apply { replyTo = reply }) }; handler.postDelayed(this,1000) }
    }
    private val notifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if(granted) startNode() else model.notice = "未启用通知，因此尚未开启设备执行。可在系统设置中授予通知权限后重试。"
    }
    private val microphone = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if(granted) model.startVoice() else model.notice="未授予麦克风权限，语音尚未启动。"
    }
    private val reminderPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if(granted) startReminders() else model.notice="系统通知权限未开启，提醒仍保存在中心服务。"
    }
    private fun startReminders() {
        getSharedPreferences("reminder-status",0).edit().putBoolean("enabled",true).apply()
        runCatching { startForegroundService(Intent(this,ai.nexus.android.reminders.ReminderService::class.java)) }
            .onFailure {
                getSharedPreferences("reminder-status",0).edit().putBoolean("enabled",false).apply()
                model.notice="提醒服务未能启动，请保持 App 在前台后重试。"
            }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        model = ViewModelProvider(this)[AppModel::class.java]
        if(intent.getBooleanExtra("open_reminders",false)) model.page=4
        setContent {
            LaunchedEffect(model.deviceStartRequest) {
                if(model.deviceStartRequest > 0) {
                    model.deviceStartHandled()
                    requestNodeStart()
                }
            }
            NexusApp(model,nodeState,nodeRunning,
            onVoiceStart = { if(checkSelfPermission(Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED) microphone.launch(Manifest.permission.RECORD_AUDIO) else model.startVoice() },
            onStartNode = { requestNodeStart() },
            onStopNode = { runCatching { service?.send(Message.obtain(null,2)) }; stopService(Intent(this,NodeService::class.java)) },
            onNotificationSettings = { startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) },
            onStartReminders = { if(Build.VERSION.SDK_INT>=33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED) reminderPermission.launch(Manifest.permission.POST_NOTIFICATIONS) else startReminders() }) }
    }
    private fun requestNodeStart() {
        if(Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            notifications.launch(Manifest.permission.POST_NOTIFICATIONS) else startNode()
    }
    private fun startNode() {
        runCatching { startForegroundService(Intent(this,NodeService::class.java)) }
            .onFailure { model.notice = "设备服务未能启动，请保持 App 在前台后重试。" }
    }
    override fun onStart() {
        super.onStart(); model.foreground(true)
        bound = bindService(Intent(this,NodeService::class.java),connection,Context.BIND_AUTO_CREATE)
        handler.post(check)
    }
    override fun onStop() {
        model.foreground(false); handler.removeCallbacks(check)
        if(bound) { unbindService(connection); bound = false; service = null }
        super.onStop()
    }
}
