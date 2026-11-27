#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
exec "${PYTHON_BIN:-python3}" "$script_dir/setup_plugin.py" "$@"
