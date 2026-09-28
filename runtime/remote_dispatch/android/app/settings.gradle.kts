// Standalone Gradle module for the REMOR dispatch target app.
// Builds with the same local toolchain as remor_app (Gradle 8.10.2,
// JDK 17, AGP 8.5.2) and ONLY the local file-based Maven repo --
// no network plugin resolution. Framework APIs only (no AndroidX),
// so the only resolved artifact is the AGP itself.
pluginManagement {
    repositories {
        maven { url = uri("file:///home/hatch/workspace/remor_mobile/local_maven_repo") }
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven { url = uri("file:///home/hatch/workspace/remor_mobile/local_maven_repo") }
    }
}
rootProject.name = "dispatch-target"
include(":app")
