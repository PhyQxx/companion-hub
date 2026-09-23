#!/usr/bin/env bash
# 下载 sherpa-onnx 流式 Zipformer 中英双语 int8 模型（docs/04 §6.4 P1）。
# 用法：bash server/scripts/download_sherpa_model.sh [目标目录]
# 默认目标 models/sherpa-streaming-zipformer（与 Admin「语音服务」的默认模型目录一致）。
set -euo pipefail

TARGET_DIR="${1:-models/sherpa-streaming-zipformer}"
BASE_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
ARCHIVE="sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20.tar.bz2"

if [ -f "${TARGET_DIR}/tokens.txt" ]; then
  echo "模型已存在：${TARGET_DIR}（如需重新下载请先删除该目录）"
  exit 0
fi

echo "下载 ${ARCHIVE}（约 70MB）……"
mkdir -p "$(dirname "${TARGET_DIR}")"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
curl -fL --retry 3 -o "${TMP_DIR}/${ARCHIVE}" "${BASE_URL}/${ARCHIVE}"

echo "解压到 ${TARGET_DIR}……"
tar -xjf "${TMP_DIR}/${ARCHIVE}" -C "${TMP_DIR}"
INNER="$(find "${TMP_DIR}" -maxdepth 1 -type d ! -path "${TMP_DIR}" | head -1)"
rm -rf "${TARGET_DIR}"
mv "${INNER}" "${TARGET_DIR}"

echo "完成。文件清单："
ls -lh "${TARGET_DIR}"
echo
echo "下一步："
echo "  1. uv sync --extra voice   # 安装 sherpa-onnx（voice 可选依赖组）"
echo "  2. Admin → 模型与路由 → 语音服务 → 提供方选「sherpa-onnx 流式 · 本地」"
echo "     模型填 ${TARGET_DIR}，保存即生效"
