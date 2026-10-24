package ai.nexus.android.node

import android.content.Context
import ai.nexus.android.client.coreUrl
import ai.nexus.protocol.*
import kotlinx.serialization.json.*
import okhttp3.*
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class ReceiveCancelled : Exception()

/** The app-private inbox is the only Android workspace; received files are never executed. */
class FileReceiver(private val context: Context, private val config: NodeConfig, private val client: OkHttpClient) {
    private val cancelled = AtomicBoolean(false)
    @Volatile private var call: Call? = null
    fun cancel() { cancelled.set(true); call?.cancel() }
    fun receive(command: Execute): JsonObject {
        val args=command.arguments
        val expected=args.getValue("size_bytes").jsonPrimitive.int
        val hash=args.getValue("sha256").jsonPrimitive.content
        val relative=args.getValue("path").jsonPrimitive.content
        val root=File(context.filesDir,"received").apply{mkdirs()}.canonicalFile
        val target=File(root,relative)
        require(target.canonicalPath == root.path+"/"+relative)
        val inbox=File(root,"NEXUS-Inbox")
        check(inbox.mkdir() || inbox.isDirectory)
        // Claim a fresh request directory exclusively. Android's app sandbox
        // forbids hard links; no other UID can write here and the sole Node
        // executor never reuses a claimed directory, including after a crash.
        check(target.parentFile!!.mkdir()) { "该次传输目录已存在，保留原件并核对回执" }
        val temporary=File.createTempFile(".receive-",".tmp",target.parentFile)
        try {
            val request=Request.Builder().url(coreUrl(config.url).newBuilder().encodedPath("/api/v1/file-transfers/${command.attempt_id}/content").build())
                .header("Authorization","Bearer ${config.token}").header("Accept-Encoding","identity").build()
            val active=client.newCall(request);call=active
            active.timeout().timeout(command.execution_timeout_seconds.toLong(),TimeUnit.SECONDS)
            if(cancelled.get()) throw ReceiveCancelled()
            val digest=MessageDigest.getInstance("SHA-256")
            var total=0
            active.execute().use { response ->
                require(response.isSuccessful && response.header("X-Content-SHA256")==hash && response.header("Content-Encoding","identity")=="identity")
                FileOutputStream(temporary).use { output ->
                    requireNotNull(response.body).byteStream().use { input ->
                        val buffer=ByteArray(65536)
                        while(true) {
                            if(cancelled.get()) throw ReceiveCancelled()
                            val count=input.read(buffer);if(count<0) break
                            total+=count;require(total<=expected)
                            output.write(buffer,0,count);digest.update(buffer,0,count)
                        }
                    }
                    output.fd.sync()
                }
            }
            require(total==expected && digest.digest().joinToString(""){"%02x".format(it)}==hash)
            if(cancelled.get()) throw ReceiveCancelled()
            check(temporary.renameTo(target)) { "文件保存未完成" }
            val readback=MessageDigest.getInstance("SHA-256")
            target.inputStream().use { input -> val b=ByteArray(65536);while(true){val n=input.read(b);if(n<0)break;readback.update(b,0,n)} }
            require(target.length()==expected.toLong() && readback.digest().joinToString(""){"%02x".format(it)}==hash)
            return obj("root_id" to str("workspace"),"attachment_id" to args.getValue("attachment_id"),"path" to str(relative),"sha256" to str(hash),"bytes_written" to JsonPrimitive(total))
        } catch(error: Exception) {
            if(cancelled.get() && !target.exists()) throw ReceiveCancelled()
            throw error
        } finally { call=null;temporary.delete() }
    }
}
