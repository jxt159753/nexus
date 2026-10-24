package ai.nexus.protocol

import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant

class WireTest {
    private fun fixtures() = wireJson.parseToJsonElement(javaClass.getResource("/commands.json")!!.readText()).jsonArray
    @Test fun pythonDigestsMatchIncludingDefaultsAndFractionalSeconds() {
        for(value in fixtures()) {
            val fixture = value.jsonObject
            val envelope = parseEnvelope(fixture.getValue("wire").jsonPrimitive.content)
            val command = parseExecute(envelope.payload,fixture.getValue("node_id").jsonPrimitive.content,envelope.payload.getValue("session_id").jsonPrimitive.content)
            assertEquals(fixture.getValue("identity").jsonPrimitive.content,identity(command,fixture.getValue("node_id").jsonPrimitive.content))
        }
    }
    @Test fun rejectsTamperingUnknownFieldsWrongSessionAndOversize() {
        val fixture = fixtures().first().jsonObject
        val envelope = parseEnvelope(fixture.getValue("wire").jsonPrimitive.content)
        val node = fixture.getValue("node_id").jsonPrimitive.content
        val session = envelope.payload.getValue("session_id").jsonPrimitive.content
        for(payload in listOf(JsonObject(envelope.payload + ("tool_version" to JsonPrimitive(2))),JsonObject(envelope.payload + ("extra" to JsonPrimitive(true))),JsonObject(envelope.payload + ("arguments" to obj("limit" to JsonPrimitive(99)))))) {
            assertThrows(Exception::class.java) { parseExecute(payload,node,session) }
        }
        assertThrows(Exception::class.java) { parseExecute(envelope.payload,node,"wrong") }
        assertThrows(Exception::class.java) { parseExecute(JsonObject(envelope.payload+("tool_version" to str("1"))),node,session) }
        val raw=wireJson.parseToJsonElement(fixture.getValue("wire").jsonPrimitive.content).jsonObject
        assertThrows(Exception::class.java) { parseEnvelope(JsonObject(raw-"protocol_version").toString()) }
        assertThrows(Exception::class.java) { parseUtc("2026-10-23T01:00:00+01:00") }
        assertThrows(Exception::class.java) { parseEnvelope(" ".repeat(MAX_MESSAGE+1)) }
    }
    @Test fun utcUsesPythonMicrosecondsAndCanonicalUnicode() {
        assertEquals("2026-10-23T01:02:03.123000Z",utc(Instant.parse("2026-10-23T01:02:03.123Z")))
        assertEquals("{\"a\":\"中文\\n\",\"z\":1}",canonical(obj("z" to JsonPrimitive(1),"a" to str("中文\n"))))
    }
    @Test fun notificationRedactionAndBounds() {
        assertEquals("",redactNotification("银行验证码","123456").text)
        assertEquals("",redactNotification("OTP","aB12Cd").text)
        assertFalse(redactNotification("家人","到家了").redacted)
        assertFalse(redactNotification("快递","取件 657889").text.contains("657889"))
        assertEquals(500,redactNotification("通知","字".repeat(1000)).text.length)
    }
}
