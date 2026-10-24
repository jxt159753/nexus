package ai.nexus.android.voice

import android.annotation.SuppressLint
import android.content.Context
import android.media.*
import android.os.Handler
import android.os.Looper
import kotlinx.coroutines.*

interface VoiceMedia {
    fun prepare()
    suspend fun capture(send: (ByteArray) -> Unit)
    fun stopCapture()
    suspend fun play(pcm: ByteArray)
    suspend fun drain()
    fun flush()
    fun close()
}

/** Foreground, half-duplex PCM. No microphone remains open while the assistant speaks. */
class PcmMedia(context: Context, private val onLoss: () -> Unit) : VoiceMedia {
    private val manager=context.getSystemService(AudioManager::class.java)
    private val attributes=AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()
    private val focus=AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE).setAudioAttributes(attributes)
        .setOnAudioFocusChangeListener({ if(it!=AudioManager.AUDIOFOCUS_GAIN) onLoss() },Handler(Looper.getMainLooper())).build()
    private var recording: AudioRecord?=null
    private var output: AudioTrack?=null
    private var written=0L
    private var outputEpoch=0
    private var closed=false
    private var watched=false
    private var deviceIds=emptySet<Int>()
    private val devices=object: AudioDeviceCallback() {
        override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>?) = changed()
        override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>?) = changed()
        fun changed() { val current=manager.getDevices(AudioManager.GET_DEVICES_INPUTS or AudioManager.GET_DEVICES_OUTPUTS).map{it.id}.toSet();if(watched && current!=deviceIds) onLoss();deviceIds=current;watched=true }
    }
    @Synchronized override fun prepare() {
        check(!closed)
        check(manager.requestAudioFocus(focus)==AudioManager.AUDIOFOCUS_REQUEST_GRANTED) { "音频设备正被占用" }
        manager.registerAudioDeviceCallback(devices,Handler(Looper.getMainLooper()))
        val size=AudioTrack.getMinBufferSize(22050,AudioFormat.CHANNEL_OUT_MONO,AudioFormat.ENCODING_PCM_16BIT)
        check(size>0)
        output=AudioTrack.Builder().setAudioAttributes(attributes).setAudioFormat(AudioFormat.Builder().setSampleRate(22050).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
            .setBufferSizeInBytes(maxOf(size,8820)).setTransferMode(AudioTrack.MODE_STREAM).build().also{check(it.state==AudioTrack.STATE_INITIALIZED);it.play()}
    }
    @SuppressLint("MissingPermission") // Runtime permission and current foreground are checked before activation.
    override suspend fun capture(send: (ByteArray) -> Unit) {
        val recorder=synchronized(this) {
            check(!closed && recording==null)
            val size=AudioRecord.getMinBufferSize(16000,AudioFormat.CHANNEL_IN_MONO,AudioFormat.ENCODING_PCM_16BIT)
            check(size>0)
            AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION,16000,AudioFormat.CHANNEL_IN_MONO,AudioFormat.ENCODING_PCM_16BIT,maxOf(size,6400))
                .also { check(it.state==AudioRecord.STATE_INITIALIZED);recording=it;it.startRecording() }
        }
        val buffer=ByteArray(3200)
        var offset=0
        try {
            withTimeout(60000) {
                while(currentCoroutineContext().isActive) {
                    val read=synchronized(this) { if(recording!==recorder || closed) return@withTimeout;recorder.read(buffer,offset,buffer.size-offset,AudioRecord.READ_NON_BLOCKING) }
                    check(read>=0) { "麦克风已断开" }
                    offset+=read
                    if(offset==buffer.size) { send(buffer.copyOf());offset=0 }
                    delay(10)
                }
            }
        } finally { synchronized(this) { if(recording===recorder) stopCapture() } }
    }
    @Synchronized override fun stopCapture() { recording?.let { runCatching{it.stop()};it.release() };recording=null }
    override suspend fun play(pcm: ByteArray) {
        val epoch=synchronized(this){outputEpoch}
        var offset=0
        withTimeout(3000) {
            while(offset<pcm.size) {
                val count=synchronized(this) {
                    if(closed || epoch!=outputEpoch) return@withTimeout
                    requireNotNull(output).write(pcm,offset,pcm.size-offset,AudioTrack.WRITE_NON_BLOCKING).also { if(it>0) written+=it/2 }
                }
                check(count>=0) { "播放设备已断开" };offset+=count
                if(count==0) delay(10)
            }
        }
    }
    override suspend fun drain() {
        val epoch=synchronized(this){outputEpoch}
        withTimeout(3000) {
            while(synchronized(this){ !closed && epoch==outputEpoch && ((output?.playbackHeadPosition ?: 0).toLong() and 0xffffffffL)<written }) delay(10)
        }
    }
    @Synchronized override fun flush() {
        outputEpoch++
        output?.let { it.pause();it.flush();it.stop();written=0;if(!closed) it.play() }
    }
    @Synchronized override fun close() {
        if(closed) return
        closed=true;stopCapture();flush();output?.release();output=null
        manager.abandonAudioFocusRequest(focus);manager.unregisterAudioDeviceCallback(devices)
    }
}
