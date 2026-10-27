package ai.nexus.android.node

import android.accessibilityservice.AccessibilityService
import android.app.KeyguardManager
import android.content.Intent
import android.graphics.Rect
import android.os.Bundle
import android.os.PowerManager
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import ai.nexus.protocol.*
import kotlinx.serialization.json.*

/** Navigation has no target control, but must still refer to a recent, unchanged window. */
internal data class PhoneNavigationObservation(val digest: String, val packageName: String, val windowId: Int, val observedAt: Long) {
    fun matches(reference: String, state: JsonObject, now: Long): Boolean = reference==digest &&
        now-observedAt in 0L..120_000L && state.string("package_name")==packageName &&
        state.getValue("window_id").jsonPrimitive.int==windowId
}

/** Passive until an authenticated Node command requests one bounded operation. */
class PhoneAccessibility: AccessibilityService() {
    companion object {
        @Volatile private var current: PhoneAccessibility? = null
        val connected: Boolean get() = current != null
        fun execute(tool: String, args: JsonObject, operation: PhoneOperation): JsonObject =
            requireNotNull(current) { "请先在系统设置开启 NEXUS 无障碍服务。" }.executeLocal(tool,args,operation)
    }
    override fun onServiceConnected() { current = this }
    override fun onAccessibilityEvent(event: AccessibilityEvent?) { /* No background screen collection. */ }
    override fun onInterrupt() { }
    override fun onUnbind(intent: Intent?): Boolean { if(current === this) current=null; return super.onUnbind(intent) }
    override fun onDestroy() { screen.clear();if(current === this) current=null; super.onDestroy() }
    private val screen by lazy { PhoneScreen(this) }
    private var navigationObservation: PhoneNavigationObservation? = null

    private val actions = linkedMapOf("click" to AccessibilityNodeInfo.ACTION_CLICK,
        "long_click" to AccessibilityNodeInfo.ACTION_LONG_CLICK, "set_text" to AccessibilityNodeInfo.ACTION_SET_TEXT,
        "scroll_forward" to AccessibilityNodeInfo.ACTION_SCROLL_FORWARD, "scroll_backward" to AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD)

    private fun bounded(value: CharSequence?, limit: Int): String = value?.toString()?.let {
        it.substring(0,it.offsetByCodePoints(0,minOf(limit,it.codePointCount(0,it.length))))
    } ?: ""

    private class Snapshot(val state: JsonObject, val handles: Map<String,AccessibilityNodeInfo>): AutoCloseable {
        override fun close() { handles.values.forEach { it.recycle() } }
    }
    private fun snapshot(operation: PhoneOperation): Snapshot {
        operation.check()
        check(getSystemService(PowerManager::class.java).isInteractive && !getSystemService(KeyguardManager::class.java).isKeyguardLocked) { "手机已锁屏，请先由用户解锁。" }
        val root = requireNotNull(rootInActiveWindow) { "当前界面没有可读取的无障碍内容。" }
        val nodes = mutableListOf<JsonObject>()
        val handles = mutableMapOf<String,AccessibilityNodeInfo>()
        var truncated = false
        var visited = 0
        var bytes = 0
        fun visit(node: AccessibilityNodeInfo, id: String, depth: Int) {
            try {
                operation.check()
                if(++visited > 600 || depth > 20 || nodes.size >= 100 || bytes >= 23500) { truncated=true;return }
                if(node.isVisibleToUser) {
                    val rect=Rect(); node.getBoundsInScreen(rect)
                    val available = if(node.isPassword || !node.isEnabled) emptyList() else actions.filterValues { value -> node.actionList.any { it.id == value } }.keys.toList()
                    val entry=obj("node" to str(id), "text" to str(if(node.isPassword) "" else bounded(node.text,300)),
                        "description" to str(if(node.isPassword) "" else bounded(node.contentDescription,300)),
                        "class_name" to str(bounded(node.className,200)), "bounds" to JsonArray(listOf(rect.left,rect.top,rect.right,rect.bottom).map(::JsonPrimitive)),
                        "actions" to JsonArray(available.map(::str)), "password" to JsonPrimitive(node.isPassword))
                    val size=entry.toString().toByteArray().size
                    if(bytes+size > 23500) { truncated=true;return }
                    bytes+=size; nodes.add(entry);handles[id]=AccessibilityNodeInfo.obtain(node)
                }
                for(index in 0 until node.childCount) {
                    if(visited >= 600 || nodes.size >= 100 || bytes >= 23500) { truncated=true;break }
                    node.getChild(index)?.let { visit(it,"$id.$index",depth+1) }
                }
            } finally { node.recycle() }
        }
        val packageName=bounded(root.packageName,200).ifEmpty { "unknown" }
        val window=root.windowId
        try { visit(root,"r",0) }
        catch(failure: Exception) { handles.values.forEach { it.recycle() }; throw failure }
        return Snapshot(obj("package_name" to str(packageName),"window_id" to JsonPrimitive(window),"nodes" to JsonArray(nodes),"truncated" to JsonPrimitive(truncated)),handles)
    }
    private fun result(state: JsonObject, action: String?=null, previous: String?=null): JsonObject {
        val digest=sha256(canonical(state))
        navigationObservation=PhoneNavigationObservation(digest,state.string("package_name"),state.getValue("window_id").jsonPrimitive.int,SystemClock.elapsedRealtime())
        return obj("state" to state,"observation_digest" to str(digest),"action" to (action?.let(::str) ?: JsonNull),
            "previous_digest" to (previous?.let(::str) ?: JsonNull),"verification" to str(if(action==null) "ui_observed" else "action_returned"))
    }

    private fun executeLocal(tool: String, args: JsonObject, operation: PhoneOperation): JsonObject {
        if(tool in SCREEN_TOOLS) return screen.execute(tool,args,operation)
        snapshot(operation).use { before ->
            if(tool==UI_OBSERVE) return result(before.state)
            val previous=args.string("observation_digest")
            val action=args.string("action")
            if(action in setOf("home","recents")) {
                require(navigationObservation?.matches(previous,before.state,SystemClock.elapsedRealtime())==true) {
                    "导航观察已过期或前台窗口已切换，请重新读取界面后再操作。"
                }
            } else require(previous==sha256(canonical(before.state))) { "手机界面已变化，请重新读取界面后再操作。" }
            val global=action in setOf("back","home","recents")
            val packageName=before.state.string("package_name")
            if(!global) check(packageName!=packageName() && packageName!="com.android.settings" &&
                !packageName.contains("permissioncontroller") && !packageName.contains("packageinstaller")) { "本应用和系统授权界面需要用户亲自操作。" }
            val target=if(global) null else requireNotNull(before.handles[args.string("node")]) { "该控件不在当前可见范围，请重新观察。" }
            if(target!=null) {
                val entry=before.state.getValue("nodes").jsonArray.map { it.jsonObject }.first { it.string("node")==args.string("node") }
                require(str(action) in entry.getValue("actions").jsonArray && !target.isPassword && target.isEnabled) { "该控件不支持所请求的操作。" }
            }
            operation.check()
            try {
                val accepted=if(global) performGlobalAction(when(action) { "back" -> GLOBAL_ACTION_BACK; "home" -> GLOBAL_ACTION_HOME; else -> GLOBAL_ACTION_RECENTS })
                    else target!!.performAction(actions.getValue(action),if(action=="set_text") Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,args.string("text")) } else null)
                if(!accepted) throw IllegalStateException("系统未接受该动作。")
                // Observe again on the next command as well: asynchronous app work may continue.
                snapshot(operation).use { after -> return result(after.state,action,previous) }
            } catch(failure: Exception) { throw PhoneEffectUnknown("手机操作已提交，但无法确认最终界面；请重新观察，不要重发原动作。",failure) }
        }
    }
    private fun packageName() = applicationContext.packageName
}
