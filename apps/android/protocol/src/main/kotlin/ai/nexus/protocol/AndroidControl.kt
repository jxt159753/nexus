package ai.nexus.protocol

import kotlinx.serialization.json.*

const val UI_OBSERVE = "android.ui_observe"
const val UI_ACT = "android.ui_act"
const val FILES = "android.files"
const val FILE_PREVIEW = "android.file_preview"
const val FILE_WRITE = "android.file_write"
const val SCREEN_OBSERVE = "android.screen_observe"
const val SCREEN_ACT = "android.screen_act"
val SCREEN_TOOLS = setOf(SCREEN_OBSERVE, SCREEN_ACT)
val PHONE_TOOLS = SCREEN_TOOLS + setOf(UI_OBSERVE, UI_ACT, FILES, FILE_PREVIEW, FILE_WRITE)
val UI_ACTIONS = setOf("click", "long_click", "set_text", "scroll_forward", "scroll_backward", "back", "home", "recents")

fun JsonObject.string(key: String): String = getValue(key).jsonPrimitive.also { require(it.isString) }.content
fun documentPath(value: String, directory: Boolean = false) {
    if (directory && value.isEmpty()) return
    require(value.isNotEmpty() && value.toByteArray().size <= 512 && value.none { it.code < 32 || it == '\\' } && value.split('/').none { it in setOf("", ".", "..") })
}
fun requireDigest(value: JsonElement?) { require(value is JsonPrimitive && value.isString && value.content.matches(Regex("[a-f0-9]{64}"))) }

/** Apply Python's schema defaults before computing the shared command digest. */
fun phoneArguments(tool: String, input: JsonObject): JsonObject {
    val a = input.toMutableMap()
    fun defaults(vararg values: Pair<String, JsonElement>) { values.forEach { (k,v) -> a.putIfAbsent(k,v) } }
    when (tool) {
        SCREEN_OBSERVE -> {
            defaults("region" to JsonNull)
            require(a.keys==setOf("region"))
            if(a["region"]!=JsonNull) {
                val r=a["region"] as? JsonArray ?: error("Invalid region")
                require(r.size==4);val v=r.map { strictInteger(it,0..8192) }
                require(v[0]<v[2] && v[1]<v[3])
            }
        }
        SCREEN_ACT -> {
            defaults("end_x" to JsonNull,"end_y" to JsonNull)
            require(a.keys==setOf("observation_digest","action","x","y","end_x","end_y"))
            requireDigest(a["observation_digest"])
            val action=JsonObject(a).string("action");require(action in setOf("tap","long_press","swipe"))
            val x=strictInteger(a["x"],0..1599);val y=strictInteger(a["y"],0..1599)
            if(action=="swipe") {
                val ex=strictInteger(a["end_x"],0..1599);val ey=strictInteger(a["end_y"],0..1599)
                require(x!=ex || y!=ey)
            } else require(a["end_x"]==JsonNull && a["end_y"]==JsonNull)
        }
        UI_OBSERVE -> require(a.isEmpty())
        UI_ACT -> {
            defaults("node" to JsonNull, "text" to JsonNull)
            require(a.keys == setOf("observation_digest", "action", "node", "text"))
            requireDigest(a["observation_digest"])
            val action = JsonObject(a).string("action").also { require(it in UI_ACTIONS) }
            val global = action in setOf("back", "home", "recents")
            require((a["node"] == JsonNull) == global && (a["text"] != JsonNull) == (action == "set_text"))
            if (!global) require(JsonObject(a).string("node").let { it.length <= 160 && it.matches(Regex("r(\\.[0-9]+)*")) })
            if (action == "set_text") require(JsonObject(a).string("text").let { it.codePointCount(0,it.length) <= 2000 })
        }
        FILES -> {
            defaults("tree_id" to JsonNull, "path" to str(""), "offset" to JsonPrimitive(0), "limit" to JsonPrimitive(50))
            require(a.keys == setOf("operation", "tree_id", "path", "offset", "limit"))
            val operation = JsonObject(a).string("operation").also { require(it in setOf("roots", "list", "read")) }
            val offset = strictInteger(a["offset"],0..1_048_576)
            strictInteger(a["limit"],1..100)
            val path = JsonObject(a).string("path")
            if (operation == "roots") require(a["tree_id"] == JsonNull && path.isEmpty() && offset == 0)
            else { requireDigest(a["tree_id"]); documentPath(path, operation == "list") }
        }
        FILE_PREVIEW, FILE_WRITE -> {
            val fields = setOf("tree_id", "path", "content") + if(tool == FILE_WRITE) setOf("expected_sha256", "preview_task_id", "preview_digest") else emptySet()
            require(a.keys == fields)
            requireDigest(a["tree_id"]); documentPath(JsonObject(a).string("path"))
            require(JsonObject(a).string("content").toByteArray().size <= 12000)
            if(tool == FILE_WRITE) {
                if(a["expected_sha256"] != JsonNull) requireDigest(a["expected_sha256"])
                strictUuid(a["preview_task_id"]); requireDigest(a["preview_digest"])
            }
        }
        else -> error("Unknown phone tool")
    }
    return JsonObject(a)
}

fun documentPreviewDigest(tree: String, path: String, previous: String?, content: String): String = sha256(canonical(obj(
    "tree_id" to str(tree), "path" to str(path), "previous_sha256" to (previous?.let(::str) ?: JsonNull), "content_sha256" to str(content))))
