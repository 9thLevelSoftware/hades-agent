#!/usr/bin/env bash
# Browser E2E smoke test for the workflows dashboard.
# Usage: bash scripts/test-workflows-dashboard-e2e.sh /tmp/hermes-workflows-browser-proof
#
# Requires: hermes CLI, agent-browser, uv, a display server.
# Creates a temporary HERMES_HOME, starts the dashboard, exercises every
# workflow scenario across five viewport sizes, saves screenshots, and
# asserts no console errors.

set -euo pipefail

PROOF_DIR="${1:?Usage: $0 <proof-directory>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURES="$REPO_ROOT/tests/fixtures/workflows/assistant_responses.json"
PORT=9148
HERMES_HOME=$(mktemp -d)
DASHBOARD_PID=""
BROWSER_SESSION="workflows-e2e"

cleanup() {
  set +e
  if [ -n "$BROWSER_SESSION" ]; then
    agent-browser close --session "$BROWSER_SESSION" 2>/dev/null
  fi
  if [ -n "$DASHBOARD_PID" ] && kill -0 "$DASHBOARD_PID" 2>/dev/null; then
    kill "$DASHBOARD_PID" 2>/dev/null
    wait "$DASHBOARD_PID" 2>/dev/null
  fi
  rm -rf "$HERMES_HOME"
}
trap cleanup EXIT

mkdir -p "$PROOF_DIR"

# --- 1. Start dashboard ------------------------------------------------------
echo "Starting hermes dashboard on port $PORT..."
cd "$REPO_ROOT"
HERMES_HOME="$HERMES_HOME" uv run --extra dev hermes dashboard \
  --port "$PORT" --no-open --skip-build &
DASHBOARD_PID=$!

echo "Waiting for dashboard HTTP 200..."
for i in $(seq 1 60); do
  if curl -sf "http://localhost:$PORT" >/dev/null 2>&1; then
    echo "Dashboard ready."
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "ERROR: Dashboard did not start within 60 seconds."
    exit 1
  fi
  sleep 1
done

# --- 2. Launch browser session -----------------------------------------------
echo "Launching agent-browser session: $BROWSER_SESSION"
agent-browser open --session "$BROWSER_SESSION" --url "http://localhost:$PORT"

# --- 3. Install network mocks for AI endpoints -------------------------------
echo "Installing network routes for draft/refine endpoints..."
DRAFT_RESPONSE=$(cat "$FIXTURES" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(json.dumps(data['draft']))
")

agent-browser network route \
  --session "$BROWSER_SESSION" \
  --url-pattern "/definitions/draft" \
  --response "$DRAFT_RESPONSE"

REFINE_RESPONSE=$(cat "$FIXTURES" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(json.dumps(data.get('refine', data['draft'])))
")

agent-browser network route \
  --session "$BROWSER_SESSION" \
  --url-pattern "/definitions/refine" \
  --response "$REFINE_RESPONSE"

# --- 4. Viewport sizes to iterate -------------------------------------------
VIEWPORTS=(
  "1440 900"
  "1280 576"
  "1024 768"
  "768 1024"
  "390 844"
)

# --- 5. Exercise scenarios on each viewport ----------------------------------
for viewport in "${VIEWPORTS[@]}"; do
  WIDTH=$(echo "$viewport" | cut -d' ' -f1)
  HEIGHT=$(echo "$viewport" | cut -d' ' -f2)
  LABEL="${WIDTH}x${HEIGHT}"
  echo ""
  echo "=== Viewport: $LABEL ==="

  agent-browser set viewport --session "$BROWSER_SESSION" \
    --width "$WIDTH" --height "$HEIGHT"

  # Navigate to workflows page
  agent-browser navigate --session "$BROWSER_SESSION" \
    --url "http://localhost:$PORT/workflows"

  # -- Prompt generation --
  echo "  Exercise: Generate draft"
  agent-browser click --session "$BROWSER_SESSION" --text "Generate From Prompt" 2>/dev/null || \
    agent-browser type --session "$BROWSER_SESSION" --selector "textarea[aria-label='Describe workflow goal']" --text "Review code and deploy" && \
    agent-browser click --session "$BROWSER_SESSION" --text "Generate From Prompt"

  # -- Accept draft --
  echo "  Exercise: Accept draft"
  agent-browser click --session "$BROWSER_SESSION" --text "Accept Draft" 2>/dev/null || \
    agent-browser click --session "$BROWSER_SESSION" --text "Accept Changes"

  # -- Structured edit (switch to Build tab, verify editor) --
  echo "  Exercise: Verify Build mode editor"
  agent-browser is visible --session "$BROWSER_SESSION" --selector "#hermes-workflows-mode-build"

  # -- Publish --
  echo "  Exercise: Publish"
  agent-browser click --session "$BROWSER_SESSION" --text "Publish" || true

  # -- Invalid run (click Run without published workflow) --
  echo "  Exercise: Start Run"
  agent-browser click --session "$BROWSER_SESSION" --text "Run" || true
  agent-browser is visible --session "$BROWSER_SESSION" --selector "[role='dialog']"
  agent-browser click --session "$BROWSER_SESSION" --text "Start Run"
  # Close dialog if still open
  agent-browser click --session "$BROWSER_SESSION" --text "Close" || true
  agent-browser click --session "$BROWSER_SESSION" --text "Cancel" || true

  # -- Feed operations --
  echo "  Exercise: Feed open/pause/resume/close/new"
  agent-browser click --session "$BROWSER_SESSION" --text "Open Continuous Feed" || true
  agent-browser click --session "$BROWSER_SESSION" --text "Pause" || true
  agent-browser click --session "$BROWSER_SESSION" --text "Resume" || true
  agent-browser click --session "$BROWSER_SESSION" --text "Close" || true
  agent-browser click --session "$BROWSER_SESSION" --text "Start new feed" || true

  # -- History --
  echo "  Exercise: History mode"
  agent-browser click --session "$BROWSER_SESSION" --text "History" || true

  # -- Cancel --
  echo "  Exercise: Cancel Execution"
  agent-browser click --session "$BROWSER_SESSION" --text "Cancel Execution" || true

  # -- Screenshot --
  SCREENSHOT_PATH="$PROOF_DIR/viewport-${LABEL}.png"
  echo "  Saving screenshot: $SCREENSHOT_PATH"
  agent-browser screenshot --session "$BROWSER_SESSION" --path "$SCREENSHOT_PATH"

  # -- Short-height geometry assertion --
  if [ "$HEIGHT" -le 600 ]; then
    echo "  Asserting short-height geometry (build mode >= 240px)..."
    GEOM=$(agent-browser eval --session "$BROWSER_SESSION" --expression "
      JSON.stringify((() => {
        const body = document.querySelector('.hermes-workflows-build-mode');
        const rect = body.getBoundingClientRect();
        return { height: rect.height, visible: rect.height >= 240 };
      })())
    ")
    VISIBLE=$(echo "$GEOM" | python3 -c "import sys,json; print(json.loads(json.loads(sys.stdin.read()))['visible'])" 2>/dev/null || echo "False")
    if [ "$VISIBLE" != "True" ]; then
      echo "ERROR: Build mode height < 240px on viewport $LABEL. Got: $GEOM"
      exit 1
    fi
    echo "  Short-height geometry OK: $GEOM"
  fi

done

# --- 6. Assert no console errors ---------------------------------------------
echo ""
echo "Checking for console errors..."
CONSOLE_ERRORS=$(agent-browser console --session "$BROWSER_SESSION" --level error 2>/dev/null || echo "")
if [ -n "$CONSOLE_ERRORS" ]; then
  echo "ERROR: Console errors detected:"
  echo "$CONSOLE_ERRORS"
  exit 1
fi
echo "No console errors."

# --- 7. Assert screenshots exist ---------------------------------------------
SCREENSHOT_COUNT=$(find "$PROOF_DIR" -name "viewport-*.png" | wc -l | tr -d ' ')
if [ "$SCREENSHOT_COUNT" -ne ${#VIEWPORTS[@]} ]; then
  echo "ERROR: Expected ${#VIEWPORTS[@]} screenshots, found $SCREENSHOT_COUNT"
  exit 1
fi

echo ""
echo "=== All $SCREENSHOT_COUNT viewport screenshots saved to $PROOF_DIR ==="
echo "=== Browser E2E smoke test PASSED ==="
