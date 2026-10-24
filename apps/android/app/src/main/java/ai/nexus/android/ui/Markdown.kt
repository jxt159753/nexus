package ai.nexus.android.ui

import android.content.Intent
import android.graphics.Color
import android.webkit.*
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import java.io.ByteArrayInputStream
import org.json.JSONObject

/** Same bundled Markdown parser and sanitizer as the browser. No native JS bridge. */
@Composable fun Markdown(text: String) {
    var height by remember { mutableStateOf(60) }
    val current by rememberUpdatedState(text)
    fun update(view: WebView) {
        val input=JSONObject.quote(current).replace("<","\\u003c").replace("\u2028","\\u2028").replace("\u2029","\\u2029")
        view.evaluateJavascript("window.showMarkdown && window.showMarkdown($input)") {
            view.postDelayed({ view.evaluateJavascript("Math.ceil(document.getElementById('content').getBoundingClientRect().bottom)") { result ->
                result.toIntOrNull()?.let { height=it.coerceIn(32,4000) }
            } },100)
        }
    }
    AndroidView(modifier=Modifier.fillMaxWidth().height(height.dp),factory={ context ->
        WebView(context).apply {
            setBackgroundColor(Color.TRANSPARENT)
            settings.javaScriptEnabled=true
            settings.allowFileAccess=false;settings.allowContentAccess=false
            settings.domStorageEnabled=false;settings.blockNetworkLoads=true
            settings.mixedContentMode=WebSettings.MIXED_CONTENT_NEVER_ALLOW
            webViewClient=object: WebViewClient() {
                override fun shouldInterceptRequest(view:WebView,request:WebResourceRequest):WebResourceResponse {
                    val uri=request.url
                    val path=uri.path.orEmpty().removePrefix("/")
                    if(uri.scheme=="https" && uri.host=="markdown.nexus.invalid" && path in setOf("index.html","entry.js","markdown.js","vendor/markdown/marked.esm.js","vendor/markdown/purify.es.mjs")) {
                        return WebResourceResponse(if(path.endsWith("html")) "text/html" else "text/javascript","UTF-8",context.assets.open("markdown/$path"))
                    }
                    return WebResourceResponse("text/plain","UTF-8",403,"Blocked",emptyMap(),ByteArrayInputStream(byteArrayOf()))
                }
                override fun shouldOverrideUrlLoading(view:WebView,request:WebResourceRequest):Boolean {
                    if(request.hasGesture() && request.url.scheme in setOf("https","http","mailto"))
                        runCatching { context.startActivity(Intent(Intent.ACTION_VIEW,request.url)) }
                    return true
                }
                override fun onPageFinished(view:WebView,url:String) { update(view) }
            }
            loadUrl("https://markdown.nexus.invalid/index.html")
        }
    },update={update(it)},onRelease={it.stopLoading();it.destroy()})
}
