package ai.nexus.protocol

data class RedactedText(val title: String, val text: String, val redacted: Boolean)

fun redactNotification(title: String, text: String): RedactedText {
    val sensitive = Regex("验证码|校验码|动态码|密码|口令|verification|one.time|password|passcode|\\botp\\b", RegexOption.IGNORE_CASE)
    if (sensitive.containsMatchIn("$title $text")) return RedactedText("[敏感通知已隐藏]", "", true)
    val digits = Regex("(?<![0-9])[0-9]{4,8}(?![0-9])")
    val safeTitle = digits.replace(title, "[已隐藏]").take(200)
    val safeText = digits.replace(text, "[已隐藏]").take(500)
    return RedactedText(safeTitle,safeText,safeTitle != title || safeText != text)
}
