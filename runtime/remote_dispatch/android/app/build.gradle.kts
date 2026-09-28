// Top-level build file. Uses buildscript{} (not the plugins{} DSL):
// the Gradle Plugin Portal's plugin-MARKER artifacts are unreachable
// from this build network, but the plugin JARs resolve fine from the
// local file-based Maven repo. Same pattern as remor_app.
buildscript {
    repositories {
        maven { url = uri("file:///home/hatch/workspace/remor_mobile/local_maven_repo") }
    }
    dependencies {
        classpath("com.android.tools.build:gradle:8.5.2")
    }
}
