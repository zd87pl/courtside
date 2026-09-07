#!/bin/sh
set -eu
# A newly mounted volume hides ownership set at image-build time. Initialize
# only the two service directories, then drop privileges before running code.
if [ "$(id -u)" = 0 ]; then
  mkdir -p "${WORK_DIR:-/data/work}" "${HF_HOME:-/data/hf}"
  chown courtside:courtside "${WORK_DIR:-/data/work}" "${HF_HOME:-/data/hf}"
  exec gosu courtside "$@"
fi
exec "$@"
