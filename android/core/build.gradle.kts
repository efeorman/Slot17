// The app's rules without Android: messages, link timing, sound decisions, status words.
// Plain Kotlin, so its tests run in seconds on any JVM and match the iOS app's Core folder.
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions { jvmTarget.set(JvmTarget.JVM_17) }
}

dependencies {
    implementation(libs.serialization.json)
    testImplementation(libs.junit)
    testImplementation(libs.kotlin.test)
}

tasks.test {
    // The fixtures both apps are tested against, at the repository root.
    systemProperty("slot17.parity", rootDir.resolve("../shared/parity").absolutePath)
    testLogging {
        events("failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
}
