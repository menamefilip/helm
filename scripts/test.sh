#!/usr/bin/env bash
set -euo pipefail
pytest -q
node --check shared-ui/app.js
