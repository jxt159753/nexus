package ai.nexus.android.node

import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.*
import ai.nexus.android.BuildConfig
import ai.nexus.protocol.*
import kotlinx.serialization.json.*

class DeviceTools(private val context: Context) {
    fun status(): JsonObject {
        val battery = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val scale = battery?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val level = battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val status = battery?.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
        val plug = battery?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0)
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        val network = connectivity.getNetworkCapabilities(connectivity.activeNetwork)
        val transport = when {
            network == null -> "none"
            network.hasTransport(NetworkCapabilities.TRANSPORT_VPN) -> "vpn"
            network.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            network.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            network.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
            else -> "other"
        }
        val storage = StatFs(context.filesDir.absolutePath)
        return obj("android_api" to JsonPrimitive(Build.VERSION.SDK_INT), "app_version" to str(BuildConfig.VERSION_NAME),
            "battery" to obj("level_percent" to (if (scale > 0 && level >= 0) JsonPrimitive((level * 100 / scale).coerceIn(0,100)) else JsonNull),
                "charging" to JsonPrimitive(status in setOf(BatteryManager.BATTERY_STATUS_CHARGING,BatteryManager.BATTERY_STATUS_FULL)),
                "plugged" to str(when(plug) { 0 -> "none"; 1 -> "ac"; 2 -> "usb"; 4 -> "wireless"; 8 -> "dock"; else -> "other" })),
            "network" to obj("connected" to JsonPrimitive(network != null), "transport" to str(transport), "metered" to (if(network == null) JsonNull else JsonPrimitive(connectivity.isActiveNetworkMetered))),
            "screen" to obj("interactive" to JsonPrimitive(context.getSystemService(PowerManager::class.java).isInteractive), "locked" to JsonPrimitive(context.getSystemService(KeyguardManager::class.java).isKeyguardLocked)),
            "storage" to obj("free_bytes" to JsonPrimitive(storage.availableBytes), "total_bytes" to JsonPrimitive(storage.totalBytes)))
    }
}
