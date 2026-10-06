import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins { id("org.jetbrains.kotlin.jvm") }

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}
kotlin { compilerOptions { jvmTarget.set(JvmTarget.JVM_17) } }

// This module is plain Kotlin compiled against the Android 7.1 (Oculus Go, API 25) framework jar so the
// MediaCodec code is type-checked in CI without an SDK. Its sources drop unchanged into an Android module.
dependencies {
    implementation(project(":core"))
    compileOnly("org.robolectric:android-all:7.1.0_r7-robolectric-r1")
}
