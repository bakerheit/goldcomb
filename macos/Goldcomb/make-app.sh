#!/bin/bash
# Build Goldcomb.app — a proper double-clickable bundle.
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release
APP="Goldcomb.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp .build/release/Goldcomb "$APP/Contents/MacOS/Goldcomb"
cp Info.plist "$APP/Contents/Info.plist"
# Ad-hoc signature so macOS runs it without complaint locally.
codesign --force -s - "$APP" >/dev/null 2>&1 || true
echo "Built $APP"
echo "Open it with:  open \"$PWD/$APP\""
