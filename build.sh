#!/usr/bin/env bash
# Build script for onshape_export_tool
# Automatically includes version (from git tag) and architecture in filename

set -e

# Get version from latest git tag, fallback to 'dev'
VERSION=$(git describe --tags --abbrev=0 2>/dev/null || echo "dev")
VERSION=${VERSION#v}  # Strip leading 'v' if present

# Get OS and architecture
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

# Build output name
OUTPUT_NAME="onshape_export_tool-v${VERSION}-${OS}-${ARCH}"

echo "Building ${OUTPUT_NAME}..."
echo "  Version: ${VERSION}"
echo "  OS: ${OS}"
echo "  Architecture: ${ARCH}"
echo ""

# Run PyInstaller
pyinstaller --onefile --name "${OUTPUT_NAME}" onshape_export_tool.py

echo ""
echo "Build complete: dist/${OUTPUT_NAME}"
