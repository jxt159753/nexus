package ai.nexus.protocol

import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class AndroidControlTest {
    @Test fun pythonCommandsHaveIdenticalDefaultedIdentity() {
        val vectors=listOf("/phone-commands.json","/screen-commands.json").flatMap { wireJson.parseToJsonElement(javaClass.getResource(it)!!.readText()).jsonArray }
        for(vector in vectors) {
            val value=vector.jsonObject
            val envelope=parseEnvelope(value.string("wire"))
            val command=parseExecute(envelope.payload,value.string("node_id"),envelope.payload.string("session_id"))
            assertEquals(value.string("identity"),identity(command,value.string("node_id")))
        }
    }
    @Test fun refusesTraversalUnboundActionsAndOversizedUtf8() {
        for(path in listOf("/sdcard/a","../a","x/../a","x\\a","x//a","x/","x\u0000a")) {
            assertThrows(IllegalArgumentException::class.java) { phoneArguments(FILES,obj("operation" to str("read"),"tree_id" to str("a".repeat(64)),"path" to str(path))) }
        }
        assertThrows(IllegalArgumentException::class.java) { phoneArguments(UI_ACT,obj("action" to str("click"),"observation_digest" to str("a".repeat(64)))) }
        assertThrows(IllegalArgumentException::class.java) { phoneArguments(FILE_PREVIEW,obj("tree_id" to str("a".repeat(64)),"path" to str("x.txt"),"content" to str("中".repeat(4001)))) }
        assertEquals(JsonNull,phoneArguments(UI_ACT,obj("action" to str("back"),"observation_digest" to str("a".repeat(64))))["node"])
        val lastPage=obj("operation" to str("read"),"tree_id" to str("a".repeat(64)),"path" to str("a.txt"),"offset" to JsonPrimitive(1_040_000))
        assertEquals(JsonPrimitive(1_040_000),phoneArguments(FILES,lastPage)["offset"])
        assertThrows(IllegalArgumentException::class.java) { phoneArguments(FILES,JsonObject(lastPage+("offset" to JsonPrimitive(1_048_577)))) }
    }
}
