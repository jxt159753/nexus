package ai.nexus.protocol

import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.*
import java.security.MessageDigest
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.UUID

val wireJson = Json { encodeDefaults = true; explicitNulls = true; ignoreUnknownKeys = false }
const val MAX_MESSAGE = 65536
const val MAX_RESULT = 32768
const val DEVICE = "android.device_status"
const val NOTIFICATIONS = "android.notification_digest"
const val RECEIVE = "filesystem.receive"

@Serializable
data class Envelope(
    val protocol_version: Int = 1,
    val type: String,
    val message_id: String = UUID.randomUUID().toString(),
    val correlation_id: String? = null,
    val sent_at: String = utc(),
    val payload: JsonObject,
)

@Serializable
data class Execute(
    val tool: String,
    val tool_version: Int,
    val arguments: JsonObject,
    val session_id: String,
    val task_id: String,
    val attempt_id: String,
    val command_digest: String,
    val grant_id: String,
    val expires_at: String,
    val execution_timeout_seconds: Int,
)

fun obj(vararg pairs: Pair<String, JsonElement>) = buildJsonObject { pairs.forEach { put(it.first, it.second) } }
fun str(value: String) = JsonPrimitive(value)
fun strictInteger(value: JsonElement?, range: IntRange): Int {
    val primitive=value as? JsonPrimitive
    require(primitive!=null && !primitive.isString && primitive.content.matches(Regex("-?[0-9]+")))
    return requireNotNull(primitive.intOrNull).also{require(it in range)}
}
fun strictUuid(value: JsonElement?): String {
    val primitive=value as? JsonPrimitive
    require(primitive!=null && primitive.isString && primitive.content.matches(Regex("[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")))
    return UUID.fromString(primitive.content).toString()
}
fun parseUtc(value: String): Instant {
    require(value.matches(Regex("[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?(Z|\\+00:00)")))
    return Instant.parse(value)
}
fun utc(instant: Instant = Instant.now()): String {
    val base = DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss").withZone(ZoneOffset.UTC).format(instant)
    val micros = instant.nano / 1000
    return base + (if (micros == 0) "" else ".${micros.toString().padStart(6, '0')}") + "Z"
}
fun sha256(value: String): String = MessageDigest.getInstance("SHA-256").digest(value.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }

/** Python json.dumps(sort_keys=True, ensure_ascii=False, separators=(',', ':')). */
fun canonical(value: JsonElement): String = when (value) {
    is JsonObject -> value.keys.sorted().joinToString(",", "{", "}") { "${JsonPrimitive(it)}:${canonical(value.getValue(it))}" }
    is JsonArray -> value.joinToString(",", "[", "]") { canonical(it) }
    else -> value.toString()
}

fun identity(command: Execute, nodeId: String): String {
    val fields = wireJson.encodeToJsonElement(command).jsonObject.toMutableMap()
    fields.remove("session_id"); fields.remove("command_digest")
    for (key in listOf("task_id", "attempt_id", "grant_id")) fields[key] = str(UUID.fromString(fields.getValue(key).jsonPrimitive.content).toString())
    fields["node_id"] = str(UUID.fromString(nodeId).toString())
    fields["expires_at"] = str(utc(Instant.parse(command.expires_at)))
    if (command.tool == NOTIFICATIONS && "limit" !in command.arguments) fields["arguments"] = obj("limit" to JsonPrimitive(10))
    if (command.tool in PHONE_TOOLS) fields["arguments"] = phoneArguments(command.tool, command.arguments)
    return canonical(JsonObject(fields))
}

fun parseEnvelope(text: String): Envelope {
    require(text.toByteArray(Charsets.UTF_8).size <= MAX_MESSAGE)
    val raw=wireJson.parseToJsonElement(text).jsonObject
    require(raw.keys==setOf("protocol_version","type","message_id","correlation_id","sent_at","payload"))
    strictInteger(raw["protocol_version"],1..1);strictUuid(raw["message_id"])
    if(raw["correlation_id"]!=JsonNull) strictUuid(raw["correlation_id"])
    val message = wireJson.decodeFromString<Envelope>(text)
    require(message.protocol_version == 1)
    UUID.fromString(message.message_id)
    parseUtc(message.sent_at)
    return message
}

fun parseExecute(payload: JsonObject, nodeId: String, sessionId: String): Execute {
    strictInteger(payload["tool_version"],1..1);strictInteger(payload["execution_timeout_seconds"],1..10)
    for(key in listOf("session_id","task_id","attempt_id","grant_id")) strictUuid(payload[key])
    val command = wireJson.decodeFromJsonElement<Execute>(payload)
    parseUtc(command.expires_at)
    require(command.session_id == sessionId && command.tool_version == 1)
    require(command.execution_timeout_seconds in 1..10)
    require(command.command_digest.matches(Regex("[a-f0-9]{64}")))
    when (command.tool) {
        DEVICE -> require(command.arguments.isEmpty())
        NOTIFICATIONS -> {
            require(command.arguments.keys.all { it == "limit" })
            val limit = command.arguments["limit"]?.jsonPrimitive
            require(limit == null || !limit.isString && limit.intOrNull in 1..20)
        }
        RECEIVE -> {
            val a=command.arguments
            require(a.keys==setOf("root_id","conversation_id","attachment_id","path","sha256","size_bytes"))
            require(a["root_id"]==str("workspace"))
            for(key in listOf("conversation_id","attachment_id")) require(strictUuid(a[key])==a[key]!!.jsonPrimitive.content)
            strictInteger(a["size_bytes"],0..16*1024*1024)
            require(a["sha256"]!!.jsonPrimitive.isString && a["sha256"]!!.jsonPrimitive.content.matches(Regex("[a-f0-9]{64}")))
            val path=a["path"]!!.jsonPrimitive.also{require(it.isString)}.content
            val parts=path.split('/')
            require(path.length<=240 && parts.size==3 && parts[0]=="NEXUS-Inbox")
            require(strictUuid(str(parts[1]))==parts[1])
            require(parts[2].isNotEmpty() && parts[2] !in setOf(".","..") && !parts[2].endsWith('.') && !parts[2].endsWith(' '))
            require(parts[2].none{it.code<32 || it.code==127 || it in "\\:*?<>|\""})
        }
        in PHONE_TOOLS -> phoneArguments(command.tool, command.arguments)
        else -> error("Unsupported Android tool")
    }
    require(sha256(identity(command, nodeId)) == command.command_digest)
    return command
}

fun message(type: String, payload: JsonObject, correlationId: String? = null): String {
    val encoded = wireJson.encodeToString(Envelope(type = type, payload = payload, correlation_id = correlationId))
    require(encoded.toByteArray(Charsets.UTF_8).size <= MAX_MESSAGE)
    return encoded
}
