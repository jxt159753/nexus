package ai.nexus.android.ui

import android.webkit.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.LifecycleOwner
import ai.nexus.android.client.*
import kotlinx.serialization.json.*
import org.json.JSONObject
import java.io.ByteArrayInputStream

/** Read-only snapshot into bundled content. No credentials or native JS bridge. */
@Composable fun Visualization(model: AppModel, modifier: Modifier = Modifier) {
    val snapshot = buildJsonObject {
        put("nodes", JsonArray(model.nodes.take(64).map { node -> buildJsonObject {
            for(key in listOf("node_id","label","online","tools","roots")) node[key]?.let { put(key,it) }
        } }))
        put("tasks", JsonArray(model.sceneTasks.map { task -> buildJsonObject {
            for(key in listOf("task_id","node_id","status","created_at","execution_outcome")) task[key]?.let { put(key,it) }
            put("request",buildJsonObject { put("intent",task.objectAt("request").text("intent")) })
        } }))
        put("activeTurn",model.activeTurn)
        model.activity?.let { put("activeTurns",JsonArray(it.objects("turns"))) }
        put("paused",model.control.text("paused") == "true")
        put("nodesFresh",model.nodesFresh)
        put("activityFresh",model.activityFresh)
        // Restored history is not a new send event: do not replay message pulses.
        put("messageKey","")
    }.toString()
    val current by rememberUpdatedState(snapshot)
    var view by remember { mutableStateOf<WebView?>(null) }
    fun update(web:WebView) {
        val data=JSONObject.quote(current).replace("<","\\u003c").replace("\u2028","\\u2028").replace("\u2029","\\u2029")
        // WebView can finish navigation before the module graph has initialized.
        // Keep the latest read-only snapshot for entry.js to consume when ready.
        web.evaluateJavascript("window.sceneSnapshot = JSON.parse($data); window.updateScene && window.updateScene(window.sceneSnapshot)",null)
    }
    DisposableEffect(view) {
        val web=view
        val owner=web?.context as? LifecycleOwner
        val observer=LifecycleEventObserver { _,event ->
            if(event==Lifecycle.Event.ON_STOP) { web?.evaluateJavascript("window.sceneVisible && window.sceneVisible(false)",null);web?.onPause() }
            if(event==Lifecycle.Event.ON_START) { web?.onResume();web?.evaluateJavascript("window.sceneVisible && window.sceneVisible(true)",null) }
        }
        owner?.lifecycle?.addObserver(observer)
        onDispose { owner?.lifecycle?.removeObserver(observer) }
    }
    AndroidView(modifier=modifier,factory={ context -> WebView(context).apply {
        layoutParams=android.view.ViewGroup.LayoutParams(android.view.ViewGroup.LayoutParams.MATCH_PARENT,android.view.ViewGroup.LayoutParams.MATCH_PARENT)
        setBackgroundColor(android.graphics.Color.TRANSPARENT)
        settings.javaScriptEnabled=true
        settings.allowFileAccess=false;settings.allowContentAccess=false
        settings.domStorageEnabled=false;settings.blockNetworkLoads=true
        settings.mixedContentMode=WebSettings.MIXED_CONTENT_NEVER_ALLOW
        webViewClient=object:WebViewClient() {
            override fun shouldInterceptRequest(web:WebView,request:WebResourceRequest):WebResourceResponse {
                val uri=request.url;val path=uri.path.orEmpty().removePrefix("/")
                val allowed=path in setOf("index.html","entry.js","visualization.js") ||
                    path.matches(Regex("vendor/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\\.js"))
                if(uri.scheme=="https" && uri.host=="visualization.nexus.invalid" && allowed) {
                    runCatching { context.assets.open("visualization/$path") }.getOrNull()?.let {
                        return WebResourceResponse(if(path.endsWith("html")) "text/html" else "text/javascript","UTF-8",it)
                    }
                }
                return WebResourceResponse("text/plain","UTF-8",403,"Blocked",emptyMap(),ByteArrayInputStream(byteArrayOf()))
            }
            override fun shouldOverrideUrlLoading(web:WebView,request:WebResourceRequest)=true
            override fun onPageFinished(web:WebView,url:String) { update(web) }
        }
        view=this;loadUrl("https://visualization.nexus.invalid/index.html")
    }},update={update(it)},onRelease={it.stopLoading();it.destroy();view=null})
}
