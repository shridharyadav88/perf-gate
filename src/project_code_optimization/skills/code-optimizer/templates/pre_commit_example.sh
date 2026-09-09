#!/bin/sh
# Example pre-commit hook for the code-optimizer skill: static AST audit of
# staged Python files (nested loops, in-loop global loads).
#
# INSTALL (manual -- the `install-agent-skills` installer NEVER writes hooks
# on your behalf; hooks execute code on every commit and must be opt-in):
#   cp .agents/skills/code-optimizer/templates/pre_commit_example.sh \
#      .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# POLICY (fail-open on desktop, per the gate contract):
#   * hints found          -> printed, commit proceeds (exit 0), unless
#                             STRICT=1 is set, which blocks (exit 1).
#   * audit harness errors -> warning printed, commit proceeds (exit 0).
#     A hook that blocks commits when the TOOL ITSELF misfires gets
#     uninstalled within a week; CI (not this hook) is where gates close.
set -u

SKILL_DIR=".agents/skills/code-optimizer"
AUDIT="$SKILL_DIR/scripts/detectors/static_audit.py"

STAGED=$(git diff --cached --name-only --diff-filter=ACM | grep '\.py$' || true)
if [ -z "$STAGED" ]; then
  exit 0
fi

if [ ! -f "$AUDIT" ]; then
  echo "code-optimizer hook: skill not deployed at $SKILL_DIR; skipping (fail-open)."
  exit 0
fi

# shellcheck disable=SC2086 -- staged paths are intentionally word-split;
# repos with spaces in Python filenames should quote or use -z/xargs instead.
if [ "${STRICT:-0}" = "1" ]; then
  python3 "$AUDIT" --strict --files $STAGED
else
  python3 "$AUDIT" --files $STAGED
fi
CODE=$?

if [ "$CODE" -eq 2 ]; then
  echo "code-optimizer hook: audit harness errored; not blocking commit (fail-open)."
  exit 0
fi
exit "$CODE"
