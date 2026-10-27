#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_HOME="$(cd -- "$SCRIPT_DIR/.." && pwd)"
BUSINESS_DIR="$APP_HOME/java/business"
REPO_ROOT="$(cd -- "$APP_HOME/.." && pwd)"

if [[ -n "${JAVA_HOME:-}" && -x "$JAVA_HOME/bin/java" ]]; then
  export PATH="$JAVA_HOME/bin:$PATH"
elif [[ -x "$REPO_ROOT/tools/local-java/jdk17/bin/java" ]]; then
  export JAVA_HOME="$REPO_ROOT/tools/local-java/jdk17"
  export PATH="$JAVA_HOME/bin:$PATH"
fi

if ! command -v java >/dev/null 2>&1 || ! command -v javac >/dev/null 2>&1; then
  echo "[ERROR] JDK not found. Install JDK 17+ and set JAVA_HOME/PATH." >&2
  exit 1
fi
if ! command -v mvn >/dev/null 2>&1; then
  echo "[ERROR] mvn not found. Install Maven and add it to PATH." >&2
  exit 1
fi

mkdir -p "$APP_HOME/runtime/h2" "$BUSINESS_DIR/logs"
cd "$BUSINESS_DIR"

MAVEN_CACHE="$REPO_ROOT/tools/local-java/m2"
mkdir -p "$MAVEN_CACHE"
exec mvn spring-boot:run \
  "-Dmaven.repo.local=$MAVEN_CACHE" \
  -Dspring-boot.run.profiles=local \
  -Dspring-boot.run.jvmArguments="-Dfile.encoding=UTF-8" \
  -Dspring-boot.run.arguments="--ocr.base-url=${OCR_BASE_URL:-http://127.0.0.1:8000}" "$@"
