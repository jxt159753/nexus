package ai.nexus.android.node

import android.content.Context
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract as Documents
import android.provider.DocumentsContract.Document as Doc
import ai.nexus.protocol.*
import kotlinx.serialization.json.*
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.security.MessageDigest
import java.util.UUID

/** SAF document IDs stay on-device. Only stable tree hashes and relative names cross Link. */
class PhoneDocuments(private val context: Context, private val operation: PhoneOperation) {
    private val resolver=context.contentResolver
    private data class Entry(val uri: Uri, val name: String, val mime: String, val flags: Int) {
        val directory get() = mime == Doc.MIME_TYPE_DIR
        fun wire(tree: String? = null, writable: Boolean = flags and Doc.FLAG_SUPPORTS_WRITE != 0) = obj("name" to str(name),
            "kind" to str(if(directory) "directory" else "file"),"tree_id" to (tree?.let(::str) ?: JsonNull),"writable" to JsonPrimitive(writable))
    }
    private val columns=arrayOf(Doc.COLUMN_DOCUMENT_ID,Doc.COLUMN_DISPLAY_NAME,Doc.COLUMN_MIME_TYPE,Doc.COLUMN_FLAGS)
    private fun entry(tree: Uri, cursor: android.database.Cursor): Entry {
        val name=cursor.getString(1)
        require(name.isNotEmpty() && name.length<=255 && '/' !in name && '\\' !in name && name.none { it.code<32 }) { "文件提供方返回了无效名称。" }
        return Entry(Documents.buildDocumentUriUsingTree(tree,cursor.getString(0)),name,cursor.getString(2),cursor.getInt(3))
    }
    private fun info(uri: Uri): Entry {
        operation.check()
        return requireNotNull(resolver.query(uri,columns,null,null,null,operation.signal)).use { c ->
            check(c.moveToFirst()) { "文件已不存在，请刷新目录。" }; val item=entry(uri,c);check(!c.moveToNext());item
        }
    }
    private fun tree(id: String, write: Boolean = false): Uri {
        operation.check()
        val permission=resolver.persistedUriPermissions.singleOrNull { sha256(it.uri.toString())==id && it.isReadPermission && (!write || it.isWritePermission) }
        return requireNotNull(permission) { "目录授权已失效，请在设备页重新选择目录。" }.uri
    }
    private fun children(parent: Entry): List<Entry> {
        require(parent.directory) { "目标不是目录。" };operation.check()
        val uri=Documents.buildChildDocumentsUriUsingTree(parent.uri,Documents.getDocumentId(parent.uri))
        val items=requireNotNull(resolver.query(uri,columns,null,null,null,operation.signal)).use { c -> buildList {
            while(c.moveToNext()) { operation.check();check(size<2000) { "此目录条目过多，请选择更小的子目录。" };add(entry(parent.uri,c)) }
        } }
        require(items.map{it.name}.toSet().size==items.size) { "目录存在重名文件，无法安全定位。" }
        return items.sortedBy { it.name }
    }
    private fun resolve(root: Uri, path: String): Entry? {
        documentPath(path,true)
        var current=info(Documents.buildDocumentUriUsingTree(root,Documents.getTreeDocumentId(root)))
        if(path.isEmpty()) return current
        for(part in path.split('/')) current=children(current).singleOrNull { it.name==part } ?: return null
        return current
    }
    private fun bytes(entry: Entry, max: Int): ByteArray {
        check(!entry.directory && entry.flags and Doc.FLAG_VIRTUAL_DOCUMENT==0) { "此项目不能作为普通文件读取。" }
        operation.check()
        return ParcelFileDescriptor.AutoCloseInputStream(requireNotNull(resolver.openFileDescriptor(entry.uri,"r",operation.signal))).use { input ->
            val output=java.io.ByteArrayOutputStream();val buffer=ByteArray(4096)
            while(true) { operation.check();val count=input.read(buffer);if(count<0) break;check(output.size()+count<=max) { "文件超过本次操作的大小限制。" };output.write(buffer,0,count) }
            output.toByteArray()
        }
    }
    private fun text(bytes: ByteArray) = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
    private fun hash(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    fun query(input: JsonObject): JsonObject {
        val args=phoneArguments(FILES,input)
        val op=args.string("operation");val path=args.string("path");val offset=args.getValue("offset").jsonPrimitive.int
        val limit=args.getValue("limit").jsonPrimitive.int
        var entries: List<JsonObject> = emptyList();var content: String?=null;var sha: String?=null;var size: Int?=null;var next: Int?=null
        if(op=="roots") {
            operation.check()
            val roots=resolver.persistedUriPermissions.filter { it.isReadPermission && Documents.isTreeUri(it.uri) }.sortedBy { it.uri.toString() }
            entries=roots.take(limit).map { permission ->
                info(Documents.buildDocumentUriUsingTree(permission.uri,Documents.getTreeDocumentId(permission.uri))).wire(sha256(permission.uri.toString()),permission.isWritePermission)
            }
            // Android limits persisted grants; our UI permits at most 16 trees.
            check(roots.size<=limit) { "请提高 limit 以取得全部授权目录。" }
        } else {
            val root=tree(args.string("tree_id"));val found=requireNotNull(resolve(root,path)) { "未找到该路径，请先列出目录。" }
            if(op=="list") {
                val items=children(found);check(offset<=items.size) { "目录已变化，请从第一页重新读取。" }
                var pageBytes=0
                entries=items.drop(offset).take(limit).map { it.wire() }.takeWhile {
                    pageBytes+=it.toString().toByteArray().size
                    pageBytes<=24000
                }
                if(offset+entries.size<items.size) next=offset+entries.size
            } else {
                val data=bytes(found,1_048_576);val full=text(data);size=data.size;sha=hash(data)
                val points=full.codePointCount(0,full.length);require(offset<=points) { "文件已变化或偏移超出范围，请重新读取。" }
                val start=full.offsetByCodePoints(0,offset)
                var end=start;var used=0;var count=0
                while(end<full.length) { val following=full.offsetByCodePoints(end,1);val n=full.substring(end,following).toByteArray().size;if(used+n>8192) break;used+=n;count++;end=following }
                content=full.substring(start,end);if(end<full.length) next=offset+count
            }
        }
        return obj("operation" to str(op),"tree_id" to args.getValue("tree_id"),"path" to str(path),"offset" to JsonPrimitive(offset),
            "entries" to JsonArray(entries),"text" to (content?.let(::str) ?: JsonNull),"sha256" to (sha?.let(::str) ?: JsonNull),
            "size_bytes" to (size?.let(::JsonPrimitive) ?: JsonNull),"next_offset" to (next?.let(::JsonPrimitive) ?: JsonNull),"truncated" to JsonPrimitive(next!=null))
    }
    private data class Target(val root: Uri, val parent: Entry, val name: String, val old: Entry?, val previous: ByteArray?)
    private fun target(args: JsonObject): Target {
        val root=tree(args.string("tree_id"),true);val path=args.string("path");documentPath(path)
        val parent=requireNotNull(resolve(root,path.substringBeforeLast('/',""))) { "父目录不存在，请先选择已有目录。" }
        check(parent.directory && parent.flags and Doc.FLAG_DIR_SUPPORTS_CREATE!=0) { "此目录不支持创建文件。" }
        val name=path.substringAfterLast('/');val old=children(parent).singleOrNull { it.name==name }
        if(old!=null) check(!old.directory && old.flags and Doc.FLAG_SUPPORTS_RENAME!=0) { "此提供方不支持保留备份后替换文件。" }
        return Target(root,parent,name,old,old?.let { bytes(it,12000).also(::text) })
    }
    fun preview(args: JsonObject): JsonObject {
        phoneArguments(FILE_PREVIEW,args)
        val found=target(args);val old=found.previous?.let(::hash);val content=sha256(args.string("content"))
        return obj("tree_id" to args.getValue("tree_id"),"path" to args.getValue("path"),"previous_sha256" to (old?.let(::str) ?: JsonNull),
            "content_sha256" to str(content),"preview_digest" to str(documentPreviewDigest(args.string("tree_id"),args.string("path"),old,content)),
            "previous_text" to (found.previous?.let { str(text(it)) } ?: JsonNull),"bytes_written" to JsonPrimitive(args.string("content").toByteArray().size))
    }
    fun write(args: JsonObject): JsonObject {
        phoneArguments(FILE_WRITE,args)
        val found=target(args);val old=found.previous?.let(::hash);val data=args.string("content").toByteArray();val expected=hash(data)
        require((old?.let(::str) ?: JsonNull)==args["expected_sha256"] && documentPreviewDigest(args.string("tree_id"),args.string("path"),old,expected)==args.string("preview_digest")) { "文件已变化，请重新预览写入。" }
        operation.check()
        var backupPath: String?=null
        try {
            val stageName=".nexus-stage-${UUID.randomUUID()}.txt"
            val stage=requireNotNull(Documents.createDocument(resolver,found.parent.uri,"text/plain",stageName)) { "文件提供方未创建临时文件。" }
            val staged=info(stage)
            check(staged.name==stageName && staged.flags and Doc.FLAG_SUPPORTS_RENAME!=0) { "文件提供方不支持安全替换；原文件保留。" }
            operation.check()
            ParcelFileDescriptor.AutoCloseOutputStream(requireNotNull(resolver.openFileDescriptor(stage,"wt",operation.signal))).use { it.write(data);it.flush() }
            check(hash(bytes(info(stage),12000))==expected) { "临时文件读回不一致，原文件保留。" }
            operation.check();tree(args.string("tree_id"),true)
            val current=children(found.parent).singleOrNull { it.name==found.name }
            require(current?.uri==found.old?.uri && current?.let { hash(bytes(it,12000)) }==old) { "写入前文件发生变化，原文件保留。" }
            if(current!=null) {
                val backupName=".nexus-backup-${UUID.randomUUID()}.txt"
                operation.check()
                val backup=requireNotNull(Documents.renameDocument(resolver,current.uri,backupName))
                check(info(backup).name==backupName && hash(bytes(info(backup),12000))==old) { "备份核验失败，请核对目录。" }
                backupPath=args.string("path").substringBeforeLast('/',"").let { if(it.isEmpty()) backupName else "$it/$backupName" }
            }
            operation.check()
            check(children(found.parent).none { it.name==found.name }) { "目标路径出现新文件，已保留备份并停止替换。" }
            val output=requireNotNull(Documents.renameDocument(resolver,stage,found.name))
            val result=info(output)
            check(result.name==found.name && hash(bytes(result,12000))==expected) { "写入后读回不一致，请核对原文件备份。" }
            return obj("tree_id" to args.getValue("tree_id"),"path" to args.getValue("path"),"sha256" to str(expected),"bytes_written" to JsonPrimitive(data.size),"backup_path" to (backupPath?.let(::str) ?: JsonNull))
        } catch(failure: Exception) { throw PhoneEffectUnknown("文件操作已开始但未完整核验；请检查目标目录中的 .nexus-backup 或 .nexus-stage 文件，不要重发原写入。",failure) }
    }
}
