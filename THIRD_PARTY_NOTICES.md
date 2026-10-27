# 第三方资源

项目许可证只覆盖 NEXUS 自有代码。下列随源码分发的资源保留各自的版权和许可文件，不适用统一改名或删除声明。

| 资源 | 来源 | 许可与仓库位置 |
| --- | --- | --- |
| Three.js 及所用 addons | [mrdoob/three.js](https://github.com/mrdoob/three.js) | MIT；[完整声明](src/nexus/web/app/vendor/THREE-LICENSE.txt) |
| Marked | [markedjs/marked](https://github.com/markedjs/marked) | MIT；[完整声明](src/nexus/web/app/vendor/markdown/MARKED-LICENSE.txt) |
| DOMPurify | [cure53/DOMPurify](https://github.com/cure53/DOMPurify) | Apache-2.0 或 MPL-2.0；[随包声明](src/nexus/web/app/vendor/markdown/DOMPURIFY-LICENSE.txt) |
| MediaPipe Tasks Vision | [google-ai-edge/mediapipe](https://github.com/google-ai-edge/mediapipe) | Apache-2.0；[完整声明](src/nexus/web/app/vendor/mediapipe/LICENSE)，版本与摘要见[资源锁](config/ui-extension/mediapipe.lock.json) |
| MediaPipe 手部模型 | [官方模型说明](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker/index) | Apache-2.0；官方链接的 [Lite/Full 模型卡第 2 页](https://storage.googleapis.com/mediapipe-assets/Model%20Card%20Hand%20Tracking%20%28Lite_Full%29%20with%20Fairness%20Oct%202021.pdf)标明模型许可，来源和 SHA-256 见资源锁；随包保留同目录 Apache 许可 |
| Gradle Wrapper | [gradle/gradle](https://github.com/gradle/gradle) | Apache-2.0；版本见 [wrapper 配置](apps/android/gradle/wrapper/gradle-wrapper.properties) |

网页资源在 `apps/windows/ui/vendor/` 有随包副本；Android 的 Markdown 资源在 `apps/android/app/src/main/assets/markdown/vendor/`，三维资源在构建时从网页目录同步。这些副本保留相同声明。

Python、Rust、npm 与 Android 的依赖分别以 `requirements*.lock`、`apps/windows/src-tauri/Cargo.lock`、npm 的 `package-lock.json`、Android 的 `gradle.lockfile` 为版本依据。发布安装包时还须携带实际打包依赖的许可；本表不是安装包全部传递依赖的完整清单。

语音模型来源、版本、摘要和标注许可在 [voice-models.lock.json](config/voice-models.lock.json)。模型文件和 Windows 构建产物不随源码提交。MuMu、Android SDK、WebView2 和云模型服务由使用者按各自条款安装或配置，不包含其软件或服务的再授权。

`src/nexus/plugins/emulator_assets/nexus-input.apk` 是仓库内 [emulator-input](apps/android/emulator-input/) 源码构建的输入助手，版本及摘要见同目录 `input.json`；它不是第三方模拟器安装包。
