#!/bin/bash
# 一键打包：建 venv → 装依赖 → py2app → ad-hoc 签名 → 产出 dist/液态岛.app
#
# 为什么用独立 venv：打包依赖（py2app 等）不应该污染你的日常 Python 环境。
# 为什么最后要签名：没有 Apple 开发者账号时做 ad-hoc 自签名，
# 至少让包结构完整（不签的话 Gatekeeper 会直接报「已损坏」）。
set -e
cd "$(dirname "$0")"

VENV=".build-venv"
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
APPNAME="液态岛"
VERSION=$(python3 -c "import sys; sys.path.insert(0,'.'); import version; print(version.APP_VERSION)")

echo "── 1/4 准备打包环境 ──"
if [ ! -x "$VENV/bin/python" ]; then
  echo "  建 venv…"
  python3 -m venv "$VENV"
  echo "  装依赖（py2app + PyObjC，第一次会慢一点）…"
  "$VENV/bin/pip" install -q -i "$MIRROR" --upgrade pip
  "$VENV/bin/pip" install -q -i "$MIRROR" \
    py2app \
    pyobjc-framework-Cocoa \
    pyobjc-framework-Quartz \
    pyobjc-framework-WebKit \
    pyobjc-framework-ApplicationServices
else
  echo "  已有 venv，跳过安装（删掉 $VENV 可强制重装）"
fi

echo "── 2/4 生成图标（如缺失）──"
if [ ! -f "assets/AppIcon.icns" ]; then
  python3 tools/make_icon.py || "$VENV/bin/python" tools/make_icon.py
fi

echo "── 3/4 py2app 打包 ──"
rm -rf build dist
"$VENV/bin/python" setup.py py2app 2>&1 | tail -25

echo "── 4/5 签名 ──"
APP="dist/$APPNAME.app"
if [ ! -d "$APP" ]; then
  echo "❌ 没找到产物，打包失败"
  exit 1
fi
# ad-hoc 自签名（横杠 = “用本机身份签”）：没有 Apple 开发者账号时只能做到这一步。
# 作用是把包结构签完整；别人机器首次打开仍需「右键 → 打开」，见 README 说明。
codesign --force --deep --sign - "$APP" 2>&1 | tail -3 || echo "  （签名失败，不影响本地使用）"

echo "── 5/5 打 dmg ──"
DMG="dist/$APPNAME-$VERSION.dmg"
STAGE="dist/dmg-stage"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "$APPNAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo
echo "✅ 完成"
echo "   App: $APP"
du -sh "$APP"
echo "   dmg: $DMG"
du -sh "$DMG"
