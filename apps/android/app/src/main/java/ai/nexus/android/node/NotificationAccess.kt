package ai.nexus.android.node

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import ai.nexus.protocol.*
import kotlinx.serialization.json.*
import java.time.Instant

class NotificationAccess : NotificationListenerService() {
    companion object {
        @Volatile var connected = false; private set
        private var packages: Set<String> = emptySet()
        private val entries = LinkedHashMap<String, JsonObject>()
        private var overflow = false
        @Synchronized fun configure(allowed: Set<String>) { packages = allowed; entries.clear(); overflow = false }
        @Synchronized fun digest(limit: Int): JsonObject {
            check(connected && packages.isNotEmpty()) { "通知访问未开启或允许名单为空" }
            val sorted = entries.values.sortedByDescending { it.getValue("posted_at").jsonPrimitive.content }
            val selected = sorted.take(limit).toMutableList()
            fun result() = obj("entries" to JsonArray(selected), "truncated" to JsonPrimitive(overflow || selected.size < sorted.size))
            while (result().toString().toByteArray().size > MAX_RESULT && selected.isNotEmpty()) selected.removeAt(selected.lastIndex)
            return result()
        }
    }
    override fun onListenerConnected() { synchronized(Companion) { connected = true; entries.clear() } }
    override fun onListenerDisconnected() { synchronized(Companion) { connected = false; entries.clear() } }
    override fun onNotificationRemoved(sbn: StatusBarNotification) { synchronized(Companion) { entries.remove(sbn.key) } }
    override fun onNotificationPosted(sbn: StatusBarNotification) {
        synchronized(Companion) {
            if (!connected || sbn.packageName !in packages || sbn.notification.visibility == Notification.VISIBILITY_SECRET) return
            // Never query activeNotifications: only new callbacks after local opt-in.
            val extras = sbn.notification.extras
            val safe = redactNotification(extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty(), extras.getCharSequence(Notification.EXTRA_TEXT)?.toString().orEmpty())
            val item = obj("key" to str(sha256("${sbn.key}:${sbn.postTime}")), "package_name" to str(sbn.packageName),
                "title" to str(safe.title), "text" to str(safe.text), "posted_at" to str(utc(Instant.ofEpochMilli(sbn.postTime))),
                "ongoing" to JsonPrimitive(sbn.isOngoing), "redacted" to JsonPrimitive(safe.redacted))
            entries[sbn.key] = item
            if (entries.size > 100) { entries.remove(entries.keys.first()); overflow = true }
        }
    }
}
