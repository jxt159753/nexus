plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}
android {
    namespace = "ai.nexus.android"
    compileSdk { version = release(36) { minorApiLevel = 1 } }
    defaultConfig {
        applicationId = "ai.nexus.android"
        minSdk = 26
        targetSdk = 36
        versionCode = 17
        versionName = "0.1.16"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    buildFeatures { compose = true; buildConfig = true }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_21; targetCompatibility = JavaVersion.VERSION_21 }
    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    testOptions { unitTests.isReturnDefaultValues = true }
}
// Bundle the same renderer and pinned Three.js assets as Windows, without a network view.
val syncVisualization by tasks.registering(Sync::class) {
    from(rootProject.file("../site/nexus/web/app")) {
        include("visualization.js", "vendor/three*.js", "vendor/THREE-LICENSE.txt", "vendor/addons/**")
        into("visualization")
    }
    into(layout.buildDirectory.dir("generated/visualizationAssets"))
}
android.sourceSets.getByName("main").assets.directories.add(layout.buildDirectory.dir("generated/visualizationAssets").get().asFile.path)
tasks.named("preBuild").configure { dependsOn(syncVisualization) }
dependencies {
    implementation(project(":protocol"))
    implementation(platform("androidx.compose:compose-bom:2026.02.01"))
    implementation("androidx.activity:activity-compose:1.8.2")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.9.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
}
