package ai.nexus.android.node

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import ai.nexus.protocol.*
import kotlinx.serialization.json.*

data class Receipt(val attempt: String, val digest: String, val phase: String, val result: String?)

class ReceiptStore(context: Context, binding: String) : SQLiteOpenHelper(context, "node-receipts.sqlite3", null, 1) {
    init {
        val db = writableDatabase
        transaction {
            db.rawQuery("SELECT binding FROM identity", null).use { cursor ->
                if (cursor.moveToFirst()) require(cursor.getString(0) == binding) { "回执属于另一设备身份；请恢复原配置" }
                else db.execSQL("INSERT INTO identity VALUES (?)", arrayOf(binding))
            }
            // All tools are in-process. A new Node process proves they stopped,
            // but cannot prove whether the old read actually occurred.
            db.execSQL("UPDATE receipts SET phase='unknown' WHERE phase IN ('received','started')")
        }
    }
    override fun onConfigure(db: SQLiteDatabase) { db.execSQL("PRAGMA synchronous=FULL"); db.setForeignKeyConstraintsEnabled(true) }
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE identity(binding TEXT NOT NULL)")
        db.execSQL("CREATE TABLE receipts(attempt TEXT PRIMARY KEY,digest TEXT NOT NULL,phase TEXT NOT NULL,result TEXT,acked INTEGER NOT NULL DEFAULT 0)")
        db.execSQL("CREATE TABLE cancellations(attempt TEXT PRIMARY KEY)")
    }
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) { error("缺少回执数据库迁移，已停止设备执行") }
    private fun <T> transaction(block: () -> T): T {
        val db = writableDatabase
        db.beginTransaction()
        return try { val value = block(); db.setTransactionSuccessful(); value } finally { db.endTransaction() }
    }
    @Synchronized fun get(attempt: String): Receipt? = readableDatabase.rawQuery("SELECT attempt,digest,phase,result FROM receipts WHERE attempt=?", arrayOf(attempt)).use {
        if (it.moveToFirst()) Receipt(it.getString(0), it.getString(1), it.getString(2), it.getString(3)) else null
    }
    private fun count(table: String): Long = readableDatabase.rawQuery("SELECT count(*) FROM $table", null).use { it.moveToFirst(); it.getLong(0) }
    @Synchronized fun receive(command: Execute): Boolean = transaction {
        val old = get(command.attempt_id)
        if (old != null) { require(old.digest == command.command_digest) { "IDEMPOTENCY_CONFLICT" }; false }
        else {
            require(count("receipts") < 10000) { "回执已满，请先核对历史" }
            val cancelled = readableDatabase.rawQuery("SELECT 1 FROM cancellations WHERE attempt=?", arrayOf(command.attempt_id)).use { it.moveToFirst() }
            writableDatabase.execSQL("INSERT INTO receipts(attempt,digest,phase) VALUES (?,?,?)", arrayOf(command.attempt_id, command.command_digest, if(cancelled) "cancelled" else "received"))
            !cancelled
        }
    }
    @Synchronized fun phase(attempt: String, phase: String, result: JsonObject? = null) = transaction {
        val values = ContentValues().apply { put("phase", phase); result?.let { put("result", it.toString()) } }
        check(writableDatabase.update("receipts", values, "attempt=?", arrayOf(attempt)) == 1)
    }
    @Synchronized fun cancel(attempt: String) = transaction {
        val exists = readableDatabase.rawQuery("SELECT 1 FROM cancellations WHERE attempt=?", arrayOf(attempt)).use { it.moveToFirst() }
        if (!exists) { require(count("cancellations") < 10000); writableDatabase.execSQL("INSERT INTO cancellations VALUES (?)", arrayOf(attempt)) }
        writableDatabase.execSQL("UPDATE receipts SET phase='cancelled' WHERE attempt=? AND phase='received'", arrayOf(attempt))
    }
    @Synchronized fun ack(attempt: String) = transaction {
        writableDatabase.execSQL("UPDATE receipts SET acked=1 WHERE attempt=? AND phase IN ('completed','cancelled','unknown')", arrayOf(attempt))
    }
    @Synchronized fun interrupted() = transaction {
        // Called only after the synchronous in-process read has returned.
        writableDatabase.execSQL("UPDATE receipts SET phase='unknown' WHERE phase IN ('received','started')")
    }
    @Synchronized fun pending(after: String): List<Receipt> = readableDatabase.rawQuery(
        "SELECT attempt,digest,phase,result FROM receipts WHERE acked=0 AND phase IN ('completed','cancelled','unknown') AND attempt>? ORDER BY attempt LIMIT 4", arrayOf(after)
    ).use { cursor -> buildList { while(cursor.moveToNext()) add(Receipt(cursor.getString(0),cursor.getString(1),cursor.getString(2),cursor.getString(3))) } }
    fun status(attempt: String, session: String): JsonObject {
        val record = get(attempt)
        val result = record?.result?.let { JsonObject(wireJson.parseToJsonElement(it).jsonObject + ("session_id" to str(session))) }
        return obj("session_id" to str(session), "attempt_id" to str(attempt), "phase" to str(record?.phase ?: "not_found"),
            "command_digest" to (record?.digest?.let(::str) ?: JsonNull), "result" to (result ?: JsonNull),
            "execution_stopped" to JsonPrimitive(record?.phase in setOf("completed", "cancelled", "unknown")))
    }
}
