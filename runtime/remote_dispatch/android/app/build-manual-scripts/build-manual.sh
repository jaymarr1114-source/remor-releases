#!/bin/bash
# Manual APK build for the REMOR dispatch target app (no Gradle daemon).
# Uses only the governed SDK at ~/workspace/sdks/rd-target-android-1.
set -e
SDK=~/workspace/sdks/rd-target-android-1
BT=$SDK/build-tools/34.0.0
AJAR=$SDK/platforms/android-34/android.jar
# The script builds the tree it lives in: APP resolves relative to this
# script's location so a build always compiles the committed sources next
# to it -- never a stale worktree copy. (A hardcoded worktree path here
# once shipped an APK built from pre-fix sources.)
APP="$(cd "$(dirname "$0")/../app" && pwd)"
BUILD=~/workspace/rd-target-fix-1-build/build-manual
OUT_APK=~/workspace/rd-target-fix-1-build/rd-target.apk
export JAVA_HOME=$SDK/jdk17
export PATH=$JAVA_HOME/bin:$PATH

rm -rf "$BUILD"
mkdir -p "$BUILD/compiled" "$BUILD/gen" "$BUILD/classes" "$BUILD/dex"

echo "== aapt2 compile"
$BT/aapt2 compile --dir "$APP/src/main/res" -o "$BUILD/compiled/res.zip"

echo "== aapt2 link"
# aapt2 (unlike AGP) needs the package attribute on <manifest>.
sed 's|<manifest xmlns:android="http://schemas.android.com/apk/res/android">|<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n    package="com.remor.dispatchtarget">|' \
  "$APP/src/main/AndroidManifest.xml" > "$BUILD/AndroidManifest.xml"
$BT/aapt2 link -o "$BUILD/base.apk" -I "$AJAR" \
  --manifest "$BUILD/AndroidManifest.xml" \
  --java "$BUILD/gen" \
  --min-sdk-version 26 --target-sdk-version 34 \
  --version-code 2 --version-name 1.0.1 \
  "$BUILD/compiled/res.zip"

echo "== javac"
find "$APP/src/main/java" "$BUILD/gen" -name "*.java" > "$BUILD/sources.txt"
wc -l "$BUILD/sources.txt"
# NOTE: -cp (not -bootclasspath): javac needs the real JDK's
# java.lang.invoke for lambda metafactory; d8 dexes the result.
javac -encoding UTF-8 -nowarn \
  -cp "$AJAR" \
  -d "$BUILD/classes" @"$BUILD/sources.txt" 2>&1 | grep -v "bootstrap class path" | head -20 || true

echo "== d8"
$BT/d8 --lib "$AJAR" --min-api 26 --output "$BUILD/dex" \
  $(find "$BUILD/classes" -name "*.class" | tr '\n' ' ')

echo "== package dex into apk"
cp "$BUILD/base.apk" "$BUILD/unaligned.apk"
cd "$BUILD/dex" && zip -q -j "$BUILD/unaligned.apk" classes.dex && cd - > /dev/null

echo "== zipalign"
$BT/zipalign -f 4 "$BUILD/unaligned.apk" "$BUILD/aligned.apk"

echo "== sign (debug key)"
KS="$BUILD/debug.keystore"
# The build wipes $BUILD at the start (rm -rf above), so a missing
# keystore here usually means a stable key was destroyed, not a first
# build. Restore the pinned dispatch-target key before ever minting a
# fresh one: a fresh key breaks updates on installed devices.
PINNED_KS=~/workspace/keys/dispatch-target/dispatch-target-debug.keystore
if [ ! -f "$KS" ]; then
  if [ -f "$PINNED_KS" ]; then
    cp "$PINNED_KS" "$KS"
  else
    keytool -genkeypair -keystore "$KS" -alias androiddebugkey \
      -storepass android -keypass android -keyalg RSA -keysize 2048 \
      -validity 10950 -dname "CN=Android Debug,O=Android,C=US" -noprompt 2>/dev/null
  fi
fi
$BT/apksigner sign --ks "$KS" --ks-pass pass:android --out "$OUT_APK" "$BUILD/aligned.apk"
$BT/apksigner verify --print-certs "$OUT_APK" | head -3
ls -la "$OUT_APK"
echo BUILD_OK
