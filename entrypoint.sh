#!/bin/sh
set -eu
exec xvfb-run -a -s "-screen 0 1100x780x24 -nolisten tcp" python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --no-access-log
