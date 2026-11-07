# Shared by the demo scripts (sourced, not run). Card text lives here once.

COVEY_BIN="${COVEY_BIN:-covey}"
if ! COVEY_BIN="$(command -v "$COVEY_BIN")"; then
  echo "covey not found. Put it on PATH or set COVEY_BIN=/absolute/path/to/covey." >&2
  exit 1
fi
export COVEY_BIN

API_SUMMARY="Owns the orders API: the Python backend that serves /orders, its request schema and validation, and the git history of schema changes. Explains schema changes, searches the code and runs the api test suite."
API_TAGS="orders,api,backend,schema,python"
API_CAPS=(--capability explain=open --capability search=open --capability run_tests=gated)
WEB_SUMMARY="Web client for placing orders: builds requests and sends them to the backend. Has no server code."
WEB_TAGS="frontend,client,web,python"
WEB_CAPS=(--capability explain=open)

# usage: load_env <demo dir>  (reads the env.sh that setup.sh wrote)
load_env() {
  # shellcheck disable=SC1091
  . "$1/env.sh"
}
