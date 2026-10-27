package ai.nexus.android.node

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.app.KeyguardManager
import android.graphics.*
import android.hardware.display.DisplayManager
import android.os.*
import android.view.Display
import android.view.accessibility.AccessibilityNodeInfo
import ai.nexus.protocol.*
import kotlinx.serialization.json.*
import java.io.ByteArrayOutputStream
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit

/** One on-demand frame, owned only by the existing Node execution process. */
class PhoneScreen(private val service: AccessibilityService) {
    private data class Window(val pkg: String,val id: Int,val rotation: Int)
    private data class Frame(val bitmap: Bitmap,val window: Window,val passwords: List<Rect>)
    private data class Observation(val frame: Frame,val region: Rect,val width: Int,val height: Int,val digest: String,val time: Long)
    private var previous: Observation?=null
    private var lastCapture=0L

    @Synchronized fun clear() { previous?.frame?.bitmap?.recycle();previous=null }
    private fun window(): Window {
        check(service.getSystemService(PowerManager::class.java).isInteractive && !service.getSystemService(KeyguardManager::class.java).isKeyguardLocked) { "手机已锁屏，请先解锁。" }
        val root=requireNotNull(service.rootInActiveWindow) { "无法确认当前应用窗口，请重新观察。" }
        try {
            return Window(requireNotNull(root.packageName).toString(),root.windowId,
                service.getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY).rotation)
        } finally { root.recycle() }
    }
    private fun capture(operation: PhoneOperation): Frame {
        if(Build.VERSION.SDK_INT<30) throw IllegalStateException("屏幕视觉操作需要 Android 11 或更新系统。")
        while(SystemClock.elapsedRealtime()-lastCapture<400L) { operation.check();Thread.sleep(25) }
        operation.check();val before=window();lastCapture=SystemClock.elapsedRealtime()
        val result=CompletableFuture<Bitmap>()
        service.takeScreenshot(Display.DEFAULT_DISPLAY,service.mainExecutor,object: AccessibilityService.TakeScreenshotCallback {
            override fun onSuccess(value: AccessibilityService.ScreenshotResult) {
                try {
                    val hardware=requireNotNull(Bitmap.wrapHardwareBuffer(value.hardwareBuffer,value.colorSpace))
                    val copy=try { requireNotNull(hardware.copy(Bitmap.Config.ARGB_8888,true)) } finally { hardware.recycle() }
                    if(!result.complete(copy)) copy.recycle()
                } catch(failure: Exception) { result.completeExceptionally(failure) }
                finally { value.hardwareBuffer.close() }
            }
            override fun onFailure(code: Int) { result.completeExceptionally(IllegalStateException("系统未提供截图（$code）；受保护界面不能截取，请重新观察或由用户操作。")) }
        })
        val bitmap=try { result.get(4,TimeUnit.SECONDS) } catch(failure: java.util.concurrent.ExecutionException) { throw (failure.cause as? Exception ?: failure) } finally { result.cancel(false) }
        try {
            operation.check();check(before==window()) { "截屏期间窗口已切换，请重新观察。" }
            check(bitmap.width<=8192 && bitmap.height<=8192 && bitmap.width.toLong()*bitmap.height<=16_000_000)
            val passwords=mutableListOf<Rect>();var visited=0
            fun mask(node: AccessibilityNodeInfo) {
                try {
                    operation.check()
                    if(++visited>600) return
                    if(node.isPassword && node.isVisibleToUser) {
                        val rect=Rect();node.getBoundsInScreen(rect)
                        if(rect.intersect(0,0,bitmap.width,bitmap.height)) passwords.add(rect)
                    }
                    for(i in 0 until node.childCount) { if(visited>=600) break;node.getChild(i)?.let(::mask) }
                } finally { node.recycle() }
            }
            service.rootInActiveWindow?.let(::mask)
            val canvas=Canvas(bitmap);val paint=Paint().apply { color=Color.BLACK }
            passwords.forEach { canvas.drawRect(it,paint) }
            return Frame(bitmap,before,passwords)
        } catch(failure: Exception) { bitmap.recycle();throw failure }
    }
    private fun image(frame: Frame,region: Rect,operation: PhoneOperation): JsonObject {
        var scaled=Bitmap.createBitmap(frame.bitmap,region.left,region.top,region.width(),region.height())
        // createBitmap can return its source; never recycle the cached source here.
        if(scaled===frame.bitmap) scaled=frame.bitmap.copy(Bitmap.Config.ARGB_8888,false)
        try {
            val scale=minOf(1.0,1600.0/maxOf(scaled.width,scaled.height))
            if(scale<1) { val next=Bitmap.createScaledBitmap(scaled,(scaled.width*scale).toInt().coerceAtLeast(1),(scaled.height*scale).toInt().coerceAtLeast(1),true);scaled.recycle();scaled=next }
            repeat(10) {
                for(quality in listOf(85,65,45)) {
                    operation.check()
                    val output=ByteArrayOutputStream();check(scaled.compress(Bitmap.CompressFormat.JPEG,quality,output))
                    val bytes=output.toByteArray()
                    if(bytes.size<=16384) return obj("media_type" to str("image/jpeg"),"width" to JsonPrimitive(scaled.width),"height" to JsonPrimitive(scaled.height),
                        "size_bytes" to JsonPrimitive(bytes.size),"sha256" to str(java.security.MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }),"data_base64" to str(android.util.Base64.encodeToString(bytes,android.util.Base64.NO_WRAP)))
                }
                val next=Bitmap.createScaledBitmap(scaled,(scaled.width*3/4).coerceAtLeast(1),(scaled.height*3/4).coerceAtLeast(1),true);scaled.recycle();scaled=next
            }
            error("截图无法压缩到传输限制，请选择更小区域。")
        } finally { scaled.recycle() }
    }
    private fun result(frame: Frame,region: Rect,operation: PhoneOperation,action: String?=null,prior: String?=null): JsonObject {
        try {
            val image=image(frame,region,operation)
            val state=obj("package_name" to str(frame.window.pkg),"window_id" to JsonPrimitive(frame.window.id),"rotation" to JsonPrimitive(frame.window.rotation),
                "width" to JsonPrimitive(frame.bitmap.width),"height" to JsonPrimitive(frame.bitmap.height),"region" to JsonArray(listOf(region.left,region.top,region.right,region.bottom).map(::JsonPrimitive)))
            val digest=sha256(canonical(obj("state" to state,"image_sha256" to image.getValue("sha256"))))
            clear();val observed=Observation(frame,region,image.getValue("width").jsonPrimitive.int,image.getValue("height").jsonPrimitive.int,digest,SystemClock.elapsedRealtime());previous=observed
            Handler(Looper.getMainLooper()).postDelayed({ synchronized(this) { if(previous===observed) clear() } },120_000)
            return obj("state" to state,"image" to image,"observation_digest" to str(digest),"action" to (action?.let(::str) ?: JsonNull),
                "previous_digest" to (prior?.let(::str) ?: JsonNull),"verification" to str(if(action==null) "screen_observed" else "gesture_completed"))
        } catch(failure: Exception) { frame.bitmap.recycle();throw failure }
    }
    @Synchronized fun execute(tool: String,args: JsonObject,operation: PhoneOperation): JsonObject {
        operation.check()
        if(tool==SCREEN_OBSERVE) {
            val frame=capture(operation)
            val region=if(args["region"]==JsonNull) Rect(0,0,frame.bitmap.width,frame.bitmap.height) else args.getValue("region").jsonArray.map { it.jsonPrimitive.int }.let { Rect(it[0],it[1],it[2],it[3]) }
            if(!Rect(0,0,frame.bitmap.width,frame.bitmap.height).contains(region)) { frame.bitmap.recycle();error("截图区域超出当前屏幕。") }
            return result(frame,region,operation)
        }
        val observed=requireNotNull(previous) { "请先读取手机截图。" }
        check(observed.digest==args.string("observation_digest") && SystemClock.elapsedRealtime()-observed.time in 0L..120_000L) { "截图引用已失效，请重新截图。" }
        val fresh=capture(operation)
        val action=args.string("action")
        try {
            check(fresh.window==observed.frame.window && fresh.bitmap.width==observed.frame.bitmap.width && fresh.bitmap.height==observed.frame.bitmap.height) { "前台应用、窗口或屏幕方向已变化，请重新截图。" }
            val pkg=fresh.window.pkg
            check(pkg!=service.packageName && pkg!="com.android.settings" && !pkg.contains("permissioncontroller") && !pkg.contains("packageinstaller")) { "本应用及系统授权界面需要用户亲自操作。" }
            fun point(x: Int,y: Int): Point {
                require(x in 0 until observed.width && y in 0 until observed.height) { "坐标超出返回图片，请按图片像素选择目标。" }
                return Point(observed.region.left+((x+0.5)*observed.region.width()/observed.width).toInt(),observed.region.top+((y+0.5)*observed.region.height()/observed.height).toInt())
            }
            val start=point(args.getValue("x").jsonPrimitive.int,args.getValue("y").jsonPrimitive.int)
            val end=if(action=="swipe") point(args.getValue("end_x").jsonPrimitive.int,args.getValue("end_y").jsonPrimitive.int) else start
            // Compare only the target/gesture corridor. A clock or chat update
            // elsewhere in the same window does not invalidate the action.
            val area=Rect((minOf(start.x,end.x)-48).coerceAtLeast(0),(minOf(start.y,end.y)-48).coerceAtLeast(0),
                (maxOf(start.x,end.x)+49).coerceAtMost(fresh.bitmap.width),(maxOf(start.y,end.y)+49).coerceAtMost(fresh.bitmap.height))
            check((fresh.passwords+observed.frame.passwords).none { Rect.intersects(it,area) }) { "密码区域不能通过坐标操作。" }
            val old=Bitmap.createBitmap(observed.frame.bitmap,area.left,area.top,area.width(),area.height())
            val current=Bitmap.createBitmap(fresh.bitmap,area.left,area.top,area.width(),area.height())
            val same=try { old.sameAs(current) } finally { if(old!==observed.frame.bitmap) old.recycle();if(current!==fresh.bitmap) current.recycle() }
            check(same) { "目标区域已变化，请重新截图定位。" }
            operation.check()
            val path=Path().apply { moveTo(start.x.toFloat(),start.y.toFloat());if(action=="swipe") lineTo(end.x.toFloat(),end.y.toFloat()) }
            val gesture=GestureDescription.Builder().addStroke(GestureDescription.StrokeDescription(path,0,when(action) { "long_press" -> 650L;"swipe" -> 400L;else -> 80L })).build()
            val completed=CompletableFuture<Boolean>()
            check(service.dispatchGesture(gesture,object: AccessibilityService.GestureResultCallback() {
                override fun onCompleted(description: GestureDescription) { completed.complete(true) }
                override fun onCancelled(description: GestureDescription) { completed.complete(false) }
            },Handler(Looper.getMainLooper()))) { "系统未接受手势。" }
            try {
                check(completed.get(2,TimeUnit.SECONDS)) { "手势被系统中断。" };operation.check()
                val after=capture(operation)
                return result(after,Rect(0,0,after.bitmap.width,after.bitmap.height),operation,action,observed.digest)
            } catch(failure: Exception) { clear();throw PhoneEffectUnknown("手势已提交，但最终状态待核对；请重新截图，不要重发原动作。",failure) }
        } finally { fresh.bitmap.recycle() }
    }
}
