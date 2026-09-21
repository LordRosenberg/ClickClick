plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "ai.clickclick.collector"
    compileSdk = 35

    defaultConfig {
        applicationId = "ai.clickclick.collector"
        minSdk = 26
        targetSdk = 35
        versionCode = 9
        versionName = "0.4.5"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    testImplementation("junit:junit:4.13.2")
}
