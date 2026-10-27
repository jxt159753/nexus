package ai.nexus.android.client

import ai.nexus.android.BuildConfig
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.json.*
import okhttp3.*
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

const val APP_ORIGIN = "https://app.nexus.local"

fun coreUrl(raw: String): HttpUrl {
    val url = raw.trim().trimEnd('/').toHttpUrl()
    require(url.username.isEmpty() && url.password.isEmpty() && url.encodedPath == "/" && url.query == null && url.fragment == null) { "请输入 Core 根地址" }
    require(url.isHttps || BuildConfig.DEBUG && url.scheme == "http" && url.host in setOf("127.0.0.1", "localhost", "10.0.2.2")) { "Core 地址必须使用 HTTPS" }
    return url
}

class ApiFailure(val status: Int, val code: String) : IOException(when(status) {
    401 -> "登录已过期，请重新登录。"
    403 -> "此操作未获授权，请检查来源配置、设备权限或审批状态。"
    409 -> "服务端状态已变化，请刷新后核对。"
    429 -> "当前请求过多或已达到调用上限，请稍后再试。"
    else -> "中心服务暂时无法完成此操作（$status）。已有请求请先核对记录。"
})

/** No automatic retry/redirect of owner writes, including ambiguous network failures. */
class CoreClient(private val vault: Vault) {
    val http = OkHttpClient.Builder().retryOnConnectionFailure(false).followRedirects(false).followSslRedirects(false)
        .connectTimeout(10, TimeUnit.SECONDS).readTimeout(40, TimeUnit.SECONDS).callTimeout(45, TimeUnit.SECONDS).build()
    // A server can close an idle pooled connection between polling rounds.
    // Recover read requests within the same timeout; never replay owner writes.
    private val readHttp = http.newBuilder().retryOnConnectionFailure(true).build()
    var base: HttpUrl? = null; private set
    private var cookie: String? = null
    private var csrf: String? = null
    fun restore(): Boolean {
        val saved = vault.read() ?: return false
        base = coreUrl(saved.getValue("url").jsonPrimitive.content)
        cookie = saved["cookie"]?.jsonPrimitive?.contentOrNull
        csrf = saved["csrf"]?.jsonPrimitive?.contentOrNull
        return cookie != null
    }
    suspend fun login(url: String, token: String) {
        require(token.matches(Regex("[A-Za-z0-9_-]{43,128}"))) { "登录令牌格式不正确" }
        base = coreUrl(url); cookie = null; csrf = null
        val result = request("POST", "/api/v1/session", obj("token" to str(token)))
        csrf = result.getValue("csrf_token").jsonPrimitive.content
        require(cookie != null) { "中心服务未返回登录会话" }
        persist()
    }
    suspend fun checkSession() { csrf = request("GET", "/api/v1/session").getValue("csrf_token").jsonPrimitive.content; persist() }
    suspend fun logout() { try { request("DELETE", "/api/v1/session") } finally { clear() } }
    fun clear() { cookie = null; csrf = null; vault.clear() }
    private fun persist() = vault.write(obj("url" to str(base.toString()), "cookie" to str(requireNotNull(cookie)), "csrf" to str(requireNotNull(csrf))))
    suspend fun fileBytes(path:String):ByteArray = withContext(Dispatchers.IO) {
        val id="[0-9a-fA-F-]{36}"
        require(Regex("/api/v1/(plugins/deliverables/$id|chat/conversations/$id/attachments/$id/content)").matches(path))
        val url=requireNotNull(base).resolve(path)!!
        val request=Request.Builder().url(url).header("Origin",APP_ORIGIN).header("Cookie","nexus_session=${requireNotNull(cookie)}").build()
        suspendCancellableCoroutine { continuation ->
            val call=readHttp.newCall(request);continuation.invokeOnCancellation{call.cancel()}
            call.enqueue(object:Callback {
                override fun onFailure(call:Call,e:IOException) { if(continuation.isActive)continuation.resumeWithException(e) }
                override fun onResponse(call:Call,response:Response) {
                    try { response.use {
                        if(!it.isSuccessful)throw ApiFailure(it.code,"FILE_UNAVAILABLE")
                        val source=requireNotNull(it.body).source()
                        require(!source.request(16L*1024*1024+1)){"文件超过16 MiB"}
                        val bytes=source.readByteArray()
                        if(continuation.isActive)continuation.resume(bytes)
                    } } catch(error:Exception) { if(continuation.isActive)continuation.resumeWithException(error) }
                }
            })
        }
    }
    suspend fun request(method: String, path: String, data: JsonObject? = null, raw: ByteArray? = null, proof: String? = null): JsonObject = withContext(Dispatchers.IO) {
        require(path.startsWith("/api/v1/") && !path.substringBefore('?').contains(".."))
        val url = requireNotNull(base).resolve(path)!!
        require(url.host == base!!.host && url.port == base!!.port && url.scheme == base!!.scheme)
        require(raw==null || raw.size<=16*1024*1024)
        val body = if (method in setOf("GET", "HEAD")) null else raw?.toRequestBody("application/octet-stream".toMediaType()) ?: (data ?: JsonObject(emptyMap())).toString().toRequestBody("application/json".toMediaType())
        val request = Request.Builder().url(url).method(method, body).header("Origin", APP_ORIGIN)
        cookie?.let { request.header("Cookie", "nexus_session=$it") }
        csrf?.let { request.header("X-CSRF-Token", it) }
        proof?.let { request.header("X-Nexus-Device-Proof", it) }
        suspendCancellableCoroutine { continuation ->
        val call = (if(method == "GET") readHttp else http).newCall(request.build())
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) { if(continuation.isActive) continuation.resumeWithException(e) }
            override fun onResponse(call: Call, response: Response) {
            try { response.use {
            val source = requireNotNull(response.body).source()
            require(!source.request(4L * 1024 * 1024 + 1)) { "返回内容过大，请缩小查询范围" }
            val text = source.readUtf8()
            val parsed = runCatching { wireJson.parseToJsonElement(text).jsonObject }.getOrElse { throw IOException("中心服务返回了无法读取的内容") }
            if (!response.isSuccessful) throw ApiFailure(response.code, parsed["detail"]?.toString()?.take(200) ?: "HTTP_ERROR")
            if (path == "/api/v1/session" && method == "POST") {
                cookie = Cookie.parseAll(url, response.headers).singleOrNull { it.name == "nexus_session" && (it.secure || !url.isHttps) }?.value
            }
            if(continuation.isActive) continuation.resume(parsed)
            } } catch(error: Exception) { if(continuation.isActive) continuation.resumeWithException(error) }
            }
        })
        }
    }
}
