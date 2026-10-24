package ai.nexus.android

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import ai.nexus.android.node.*
import ai.nexus.protocol.*
import kotlinx.serialization.json.*
import okhttp3.OkHttpClient
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.net.ServerSocket
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID
import java.util.concurrent.Executors

@RunWith(AndroidJUnit4::class)
class FileReceiveTest {
    @Test fun binaryDownloadReadbackNoOverwriteAndBadHash() {
        val context=InstrumentationRegistry.getInstrumentation().targetContext
        for(valid in listOf(true,false)) {
            val raw=ByteArray(4*1024*1024+17){(it%256).toByte()}
            val hash=MessageDigest.getInstance("SHA-256").digest(raw).joinToString(""){"%02x".format(it)}
            val server=ServerSocket(0,1,java.net.InetAddress.getByName("127.0.0.1"))
            val executor=Executors.newSingleThreadExecutor()
            val served=executor.submit {
                server.accept().use { socket ->
                    val reader=socket.getInputStream().bufferedReader();val lines=mutableListOf<String>()
                    while(true){val line=reader.readLine() ?: break;if(line.isEmpty())break;lines.add(line)}
                    check(lines.any{it=="Authorization: Bearer "+"x".repeat(43)})
                    socket.getOutputStream().apply {
                        write("HTTP/1.1 200 OK\r\nContent-Length: ${raw.size}\r\nX-Content-SHA256: $hash\r\nConnection: close\r\n\r\n".toByteArray());write(raw);flush()
                    }
                }
            }
            val folder=UUID.randomUUID().toString();val path="NEXUS-Inbox/$folder/测试.bin"
            val args=obj("root_id" to str("workspace"),"conversation_id" to str(UUID.randomUUID().toString()),"attachment_id" to str(UUID.randomUUID().toString()),"path" to str(path),"sha256" to str(if(valid)hash else "0".repeat(64)),"size_bytes" to JsonPrimitive(raw.size))
            val id=UUID.randomUUID().toString()
            val command=Execute(RECEIVE,1,args,id,id,id,"0".repeat(64),id,utc(Instant.now().plusSeconds(60)),10)
            val config=NodeConfig("http://127.0.0.1:${server.localPort}",id,"x".repeat(43),emptySet())
            val client=OkHttpClient.Builder().retryOnConnectionFailure(false).followRedirects(false).build()
            val file=File(context.filesDir,"received/$path")
            try {
                if(valid) {
                    val result=FileReceiver(context,config,client).receive(command)
                    assertEquals(hash,result.getValue("sha256").jsonPrimitive.content)
                    assertArrayEquals(raw,file.readBytes())
                    assertThrows(Exception::class.java){FileReceiver(context,config,client).receive(command)}
                    assertArrayEquals(raw,file.readBytes())
                } else {
                    assertThrows(Exception::class.java){FileReceiver(context,config,client).receive(command)}
                    assertFalse(file.exists())
                }
            } finally {
                server.close();executor.shutdownNow();runCatching{served.get()};client.connectionPool.evictAll();client.dispatcher.executorService.shutdown()
                file.delete();file.parentFile?.listFiles()?.forEach{it.delete()};file.parentFile?.delete()
            }
        }
    }
}
