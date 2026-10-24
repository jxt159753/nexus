package ai.nexus.android

import android.content.Context
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.ext.junit.runners.AndroidJUnit4
import ai.nexus.android.node.*
import ai.nexus.android.security.Vault
import ai.nexus.protocol.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class DeviceTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext
    private fun command() = Execute(DEVICE,1,obj(),UUID.randomUUID().toString(),UUID.randomUUID().toString(),UUID.randomUUID().toString(),"a".repeat(64),UUID.randomUUID().toString(),utc(),10)
    @Test fun encryptedVaultIsAuthenticatedAndSeparate() {
        val name = "test-${UUID.randomUUID()}"
        val vault = Vault(context,name)
        val content = obj("token" to str("local-fixture-secret"))
        try {
            vault.write(content); assertEquals(content,vault.read())
            val file = java.io.File(context.noBackupFilesDir,"$name.enc")
            assertFalse(file.readText().contains("local-fixture-secret"))
            val bytes=file.readBytes();bytes[bytes.lastIndex]=(bytes.last().toInt() xor 1).toByte();file.writeBytes(bytes)
            assertThrows(Exception::class.java) { vault.read() }
        } finally { vault.clear() }
    }
    @Test fun receiptsSurviveRestartAndNeverExecuteTwice() {
        // Isolated database directory, never the App's live Node ledger.
        val isolated = object: android.content.ContextWrapper(context) {
            override fun getDatabasePath(name: String) = java.io.File(context.cacheDir,"receipt-test-${testId}/$name").apply { parentFile!!.mkdirs() }
            override fun openOrCreateDatabase(name: String, mode: Int, factory: android.database.sqlite.SQLiteDatabase.CursorFactory?) = android.database.sqlite.SQLiteDatabase.openOrCreateDatabase(getDatabasePath(name),factory)
            override fun openOrCreateDatabase(name: String, mode: Int, factory: android.database.sqlite.SQLiteDatabase.CursorFactory?, handler: android.database.DatabaseErrorHandler?) = android.database.sqlite.SQLiteDatabase.openOrCreateDatabase(getDatabasePath(name).path,factory,handler)
            val testId = UUID.randomUUID().toString()
        }
        val first=command();val done=command();val cancelled=command()
        ReceiptStore(isolated,"test-binding").use { store ->
            assertTrue(store.receive(first));store.phase(first.attempt_id,"started")
            assertFalse(store.receive(first))
            assertThrows(Exception::class.java) { store.receive(first.copy(command_digest="b".repeat(64))) }
            assertTrue(store.receive(done));store.phase(done.attempt_id,"completed",obj("session_id" to str(done.session_id),"result" to obj()))
            store.cancel(cancelled.attempt_id);assertFalse(store.receive(cancelled))
        }
        ReceiptStore(isolated,"test-binding").use { store ->
            assertEquals("unknown",store.get(first.attempt_id)!!.phase);assertFalse(store.receive(first))
            assertEquals("completed",store.get(done.attempt_id)!!.phase)
            assertEquals("cancelled",store.get(cancelled.attempt_id)!!.phase)
            assertEquals("new-session",store.status(done.attempt_id,"new-session").getValue("result").jsonObject.getValue("session_id").jsonPrimitive.content)
            assertEquals(3,store.pending("").size);store.ack(done.attempt_id);assertEquals(2,store.pending("").size)
        }
        assertThrows(Exception::class.java) { ReceiptStore(isolated,"different-identity") }
    }
    @Test fun readsRealAndroidStatusWithoutIdentifiers() {
        val result=DeviceTools(context).status()
        assertEquals(setOf("android_api","app_version","battery","network","screen","storage"),result.keys)
        assertTrue(result.getValue("storage").jsonObject.getValue("total_bytes").jsonPrimitive.long>0)
        assertThrows(Exception::class.java) { NotificationAccess.configure(emptySet());NotificationAccess.digest(1) }
    }
}
