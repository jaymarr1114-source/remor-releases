plugins {
    id("com.android.application")
}

android {
    namespace = "com.remor.dispatchtarget"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.remor.dispatchtarget"
        minSdk = 26 // dispatchGesture + TYPE_APPLICATION_OVERLAY
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
        debug {
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
