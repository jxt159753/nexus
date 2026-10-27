package ai.nexus.android.ui

fun statusLabel(value:String):String = mapOf(
    "completed" to "已完成","succeeded" to "已完成","available" to "可打开","ready" to "准备就绪",
    "verified" to "已核对","delivered" to "已送达","received" to "已接收","exported" to "可打开",
    "queued" to "等待处理","running" to "执行中","generating" to "正在回复","dispatching" to "正在发送",
    "preparing" to "准备中","sent" to "已提交","waiting" to "等待中","verifying" to "正在核对结果",
    "awaiting_approval" to "等待你确认","permission" to "等待你确认","recovering" to "正在核对进度",
    "failed" to "未完成","timed_out" to "处理超时","cancelled" to "已取消","stopped" to "已停止",
    "stopping" to "正在停止","interrupted" to "已中断","incomplete" to "回复未完整结束",
    "unknown" to "结果待核对","expired" to "已过期","rejected" to "未获批准"
)[value] ?: when(value) {
    "可打开", "已送达设备", "传输结果待核对", "传输中", "未送达设备" -> value
    "" -> ""
    else -> "状态待确认"
}

fun actionLabel(value:String):String = mapOf(
    "android_screen_observe" to "读取手机画面","android.screen_observe" to "读取手机画面",
    "android_screen_act" to "按画面操作手机","android.screen_act" to "按画面操作手机",
    "android_ui_observe" to "读取手机界面","android.ui_observe" to "读取手机界面",
    "android_ui_act" to "操作手机界面","android.ui_act" to "操作手机界面",
    "android_files" to "读取手机文件","android.files" to "读取手机文件",
    "android_file_write" to "保存手机文件","android.file_write" to "保存手机文件",
    "android_file_preview" to "预览手机文件修改","android.file_preview" to "预览手机文件修改",
    "get_system_info" to "查看电脑信息","system.info" to "查看电脑信息","list_directory" to "查看文件",
    "filesystem.list" to "查看文件","read_file" to "读取文件","filesystem.read" to "读取文件",
    "write_file" to "保存文件","filesystem.write" to "保存文件","send_file" to "发送文件","filesystem.receive" to "接收文件",
    "android.device_status" to "查看设备状态","get_device_status" to "查看设备状态",
    "android.notification_digest" to "读取已授权通知","plugin_call" to "使用扩展","plugin.call" to "使用扩展",
    "web_search" to "搜索资料","web_fetch" to "读取网页","get_node_status" to "查看设备状态",
    "run_skill" to "运行流程","skill_create" to "创建流程","command.run" to "运行已授权操作"
)[value.substringBefore('@')] ?: "处理任务"

fun fileSize(value:String):String {
    val size=value.toLongOrNull() ?: return "文件"
    return when { size>=1048576 -> "%.1f MB".format(size/1048576.0);size>=1024 -> "%.1f KB".format(size/1024.0);else -> "$size 字节" }
}
