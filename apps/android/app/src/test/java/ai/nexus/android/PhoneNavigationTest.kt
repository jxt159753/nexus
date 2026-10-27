package ai.nexus.android

import ai.nexus.android.node.PhoneNavigationObservation
import ai.nexus.protocol.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class PhoneNavigationTest {
    private fun state(pkg: String="chat.app", window: Int=7, text: String="streaming") =
        obj("package_name" to str(pkg),"window_id" to JsonPrimitive(window),"nodes" to JsonArray(listOf(obj("text" to str(text)))))
    private val observed=state(text="before")
    private val digest=sha256(canonical(observed))
    private val reference=PhoneNavigationObservation(digest,"chat.app",7,1_000L)

    @Test fun changingTextWithinObservedWindowDoesNotBlockNavigation() {
        assertNotEquals(digest,sha256(canonical(state())))
        assertTrue(reference.matches(digest,state(),2_000L))
    }
    @Test fun changingForegroundPackageOrWindowRequiresNewObservation() {
        assertFalse(reference.matches(digest,state(pkg="other.app"),2_000L))
        assertFalse(reference.matches(digest,state(window=8),2_000L))
    }
    @Test fun unknownDigestCannotAuthorizeNavigation() {
        assertFalse(reference.matches("0".repeat(64),state(),2_000L))
    }
    @Test fun observationExpiresUsingMonotonicTime() {
        assertTrue(reference.matches(digest,state(),121_000L))
        assertFalse(reference.matches(digest,state(),121_001L))
        assertFalse(reference.matches(digest,state(),999L))
    }
}
