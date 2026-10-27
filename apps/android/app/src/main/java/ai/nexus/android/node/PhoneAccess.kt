package ai.nexus.android.node

import android.content.Context
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.serialization.json.*
import java.util.concurrent.atomic.AtomicBoolean
import java.io.File
import java.io.RandomAccessFile

/** Read from disk at execution boundaries, including changes from the UI process. */
class PhoneAccess(private val context: Context) {
    companion object { private val storageLock=Any() }
    // AtomicFile needs external synchronization: another process's openRead can
    // remove an in-progress .new file. Serialize both reads and writes on disk.
    private fun <T> withStore(action: (Vault) -> T): T = synchronized(storageLock) {
        RandomAccessFile(File(context.noBackupFilesDir,"phone-access.lock"),"rw").use { file ->
            file.channel.lock().use { action(Vault(context,"phone-access")) }
        }
    }
    fun enabled(key: String): Boolean = withStore { it.read()?.get(key)?.jsonPrimitive?.boolean == true }
    fun choices(): JsonObject = withStore { it.read() ?: obj() }
    fun set(key: String, value: Boolean) {
        require(key in setOf("accessibility", "files", "vision"))
        withStore { store ->
            val next=JsonObject((store.read() ?: obj()).toMutableMap().apply { put(key,JsonPrimitive(value)) })
            store.write(next)
            check(store.read()==next) { "本机权限设置未能保存。" }
        }
    }
    fun tools(): Set<String> = buildSet {
        if(enabled("accessibility") && PhoneAccessibility.connected) {
            addAll(listOf(UI_OBSERVE,UI_ACT))
            if(enabled("vision") && android.os.Build.VERSION.SDK_INT>=30) addAll(SCREEN_TOOLS)
        }
        if(enabled("files")) {
            add(FILES)
            if(context.contentResolver.persistedUriPermissions.any { it.isReadPermission && it.isWritePermission }) addAll(listOf(FILE_PREVIEW,FILE_WRITE))
        }
    }
}

class PhoneOperation(private val allowed: () -> Boolean) {
    private val cancelled = AtomicBoolean(false)
    val signal = android.os.CancellationSignal()
    fun cancel() { cancelled.set(true); signal.cancel() }
    fun check() { check(!cancelled.get()) { "操作已停止，请核对已有执行结果。" }; check(allowed()) { "本机权限已关闭，请重新授权后再操作。" } }
}

/** A mutation may already have taken effect. Keep the receipt unknown, never retry. */
class PhoneEffectUnknown(message: String, cause: Throwable? = null): Exception(message,cause)
