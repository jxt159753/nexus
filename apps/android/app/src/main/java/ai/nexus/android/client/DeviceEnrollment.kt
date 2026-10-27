package ai.nexus.android.client

import android.content.Context
import android.os.Build
import android.util.Base64
import ai.nexus.android.security.Vault
import ai.nexus.android.node.PhoneAccess
import ai.nexus.protocol.*
import java.security.SecureRandom
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.*

/** Installation and Node secrets stay in separate native encrypted storage, never chat text. */
data class DeviceSync(val status: JsonObject, val installed: Boolean, val error: Exception? = null)

class DeviceEnrollment(context: Context, private val client: CoreClient) {
    private val store = Vault(context,"device-client")
    private val nodeStore = Vault(context,"node")
    private val access = PhoneAccess(context)
    private val syncLock = Mutex()
    private val lock = Mutex()
    private var prepared = ""
    private fun secret(): String = Base64.encodeToString(ByteArray(32).also { SecureRandom().nextBytes(it) },Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)

    suspend fun prepare(): JsonObject = lock.withLock {
        val url = requireNotNull(client.base).toString()
        var saved = store.read()
        require(saved == null || saved.text("url") == url) { "本机设备已绑定其他 Core，请回到原服务管理设备身份。" }
        if(saved == null) {
            val node = nodeStore.read()
            require(node == null || coreUrl(node.text("url")) == client.base) { "本机 Node 与其他 Core 绑定，不能覆盖原身份和回执。" }
            saved = obj("url" to str(url), "client_id" to str(uid()), "proof" to str(secret()),
                "node_id" to str(node?.text("node_id") ?: uid()), "node_token" to str(node?.text("token") ?: secret()),
                "name" to str(Build.MODEL.trim().take(40).ifEmpty { "我的 Android 设备" }))
            store.write(saved)
        }
        if(prepared != saved.text("client_id")) {
            client.request("POST","/api/v1/device-clients",JsonObject(saved.filterKeys { it in setOf("client_id","node_id","proof","node_token","name") }))
            prepared = saved.text("client_id")
        }
        saved
    }

    fun proofFor(clientId: String): String? {
        if(clientId.isEmpty()) return null
        val saved = requireNotNull(store.read()) { "设备绑定信息无法读取，请保留当前请求并核对。" }
        require(saved.text("client_id") == clientId && saved.text("url") == client.base.toString()) { "请在原设备和原 Core 核对请求。" }
        return saved.text("proof")
    }

    /** installed is true only when a newly registered identity is first installed. */
    suspend fun sync(): DeviceSync = syncLock.withLock {
        val saved = prepare()
        var status = client.request("GET","/api/v1/device-clients/${saved.text("client_id")}",proof=saved.text("proof"))
        if(status.text("registered") != "true") return@withLock DeviceSync(status,false)
        val installed = lock.withLock {
            val existing = nodeStore.read()
            require(existing == null || existing.text("node_id") == status.text("node_id") && coreUrl(existing.text("url")) == client.base) { "设备身份与本机回执不一致，未覆盖原配置。" }
            if(existing == null) {
                // No explicit local inbox choice yet. Core keeps the registration
                // permissions; the Node's default capability alone cannot grant it.
                nodeStore.write(obj("url" to str(saved.text("url")),"node_id" to str(saved.text("node_id")),
                    "token" to str(saved.text("node_token")),"packages" to JsonArray(emptyList())))
            }
            existing == null
        }
        try {
            val local = requireNotNull(nodeStore.read())
            val choices = JsonObject(access.choices() + local.filterKeys { it=="receive_files" } + mapOf(
                "notifications" to JsonPrimitive(local["packages"]?.jsonArray?.isNotEmpty() == true)))
            val current = requireNotNull(store.read())
            val pending = current.objectAt("permission_sync")
            val request = if(pending.objectAt("permissions") == choices) pending else obj(
                "revision" to JsonPrimitive((pending["revision"]?.jsonPrimitive?.long ?: 0L) + 1),
                "permissions" to choices)
            // Save before sending. Lost replies, process restarts and reconnects use
            // the same revision; they cannot restore a later remote revocation.
            if(request != pending) store.write(JsonObject(current + ("permission_sync" to request)))
            if(status.objectAt("permission_sync") != request) {
                status = client.request("PUT","/api/v1/device-clients/${saved.text("client_id")}/permissions",request,proof=saved.text("proof"))
            }
            DeviceSync(status,installed)
        } catch(cancel: CancellationException) { throw cancel }
        catch(error: Exception) { DeviceSync(status,installed,error) }
    }

    fun startFor(turn: JsonObject): Boolean {
        val change = turn.objectAt("device_change")
        val saved = store.read() ?: return false
        if(change.text("registration") != "true" || change.text("node_id") != saved.text("node_id") || saved.text("start_turn") == turn.text("turn_id")) return false
        store.write(JsonObject(saved + ("start_turn" to str(turn.text("turn_id")))))
        return true
    }
}
