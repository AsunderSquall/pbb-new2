#!/usr/bin/env bash
# 快速配置 PBB Linux 运行/编译环境。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$ROOT/runtime/miniconda"
PYTHON="$RUNTIME/bin/python"
SKIP_BUILD=0

for arg in "$@"; do
    case "$arg" in
        --skip-build) SKIP_BUILD=1 ;;
        -h|--help)
            cat <<'EOF'
用法: ./setup_env.sh [--skip-build]

创建或检查 runtime/miniconda，安装 pybind11、PyYAML，检查 g++，
并默认编译 source/build 下的 CLI 引擎和 Python 模块。
EOF
            exit 0
            ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

if [ ! -x "$PYTHON" ]; then
    installer=""
    for candidate in \
        "$ROOT/../Miniconda3-latest-Linux-x86_64.sh" \
        "$ROOT/Miniconda3-latest-Linux-x86_64.sh"; do
        if [ -f "$candidate" ]; then installer="$candidate"; break; fi
    done

    if [ -n "$installer" ]; then
        echo "[setup] 使用 Miniconda 安装器: $installer"
        bash "$installer" -b -p "$RUNTIME"
    else
        system_python="$(command -v python3 || true)"
        if [ -z "$system_python" ]; then
            echo "ERROR: 未找到 python3，也未找到 Miniconda 安装器。" >&2
            exit 1
        fi
        if ! "$system_python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
            echo "ERROR: 系统 Python 需要 3.10+；请把 Miniconda3-latest-Linux-x86_64.sh 放在项目根目录。" >&2
            exit 1
        fi
        echo "[setup] 未找到 Miniconda，使用系统 Python 创建兼容环境。"
        "$system_python" -m venv "$RUNTIME" || {
            echo "ERROR: 创建 venv 失败。Debian/Ubuntu 请先安装 python3-venv。" >&2
            exit 1
        }
    fi
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
    echo "ERROR: runtime/miniconda 中的 Python 低于 3.10，请更换环境或删除后重新运行。" >&2
    exit 1
fi

if ! command -v g++ >/dev/null 2>&1; then
    echo "ERROR: 未找到 g++。请安装编译器后重新运行：" >&2
    echo "  Debian/Ubuntu: sudo apt install build-essential" >&2
    echo "  Fedora/RHEL:   sudo dnf install gcc-c++" >&2
    echo "  Arch:          sudo pacman -S gcc" >&2
    exit 1
fi

echo "[setup] Python: $($PYTHON --version 2>&1)"
"$PYTHON" -m pip install --quiet pybind11 PyYAML
"$PYTHON" - <<'PY'
import pybind11, yaml
print(f"[setup] pybind11 {pybind11.__version__}, PyYAML {yaml.__version__}")
PY

chmod +x "$ROOT"/*.sh "$ROOT/source/run.sh" 2>/dev/null || true

if [ "$SKIP_BUILD" -eq 0 ]; then
    echo "[setup] 编译 CLI 引擎和 Python 字符集模块..."
    "$PYTHON" "$ROOT/source/rebuild_local.py"
else
    echo "[setup] 已跳过编译（--skip-build）。"
fi

echo "[setup] 环境配置完成。可运行: $ROOT/start_cli.sh --range-end 1000000 --threads 4"
