#!/bin/sh
set -eu

export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest -v skill.scripts.test_runtime
python3 -m unittest discover -s tests -v
sh -n bin/codex-harness init.sh
scripts/install-codex --dry-run
