#!/usr/bin/env bash
# Lint this repository: formatters rewrite in place, then checkers judge.
#
# Run it on the files you touched, or with no arguments to take every
# Python file in the repository root and in tests/. The exit status is
# every checker's status combined, so a clean run is exit 0 and nothing
# else. Note that the formatters rewrite in place, so a bare run is not
# a read-only check even when it reports nothing.
#
# The conventions it enforces: Google-style docstrings on everything
# public, f-strings rather than percent formatting, an explicit encoding
# on every text open, specific exceptions rather than bare Exception, and
# 79 columns. Suppression comments are not an acceptable way to pass --
# fix the code instead.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Words that may not appear anywhere in this repository, as an extended
# regex. Read from an untracked file, never written down here: a list
# that names the private things is the very leak it exists to prevent,
# which is a mistake this file has already made once.
#
# Put one pattern per line in .agentbus-deny (gitignored), or set
# AGENTBUS_DENY directly. Empty by default, so the check is inert until
# somebody says what their own private names are.
DENY_FILE="${AGENTBUS_DENY_FILE:-$SCRIPT_DIR/.agentbus-deny}"
if [ -z "${AGENTBUS_DENY:-}" ] && [ -f "$DENY_FILE" ]; then
  AGENTBUS_DENY="$(grep -vE '^\s*(#|$)' "$DENY_FILE" | paste -sd '|' -)"
fi
AGENTBUS_DENY="${AGENTBUS_DENY:-}"

TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
  mapfile -t TARGETS < <(
    find . -maxdepth 2 -name '*.py' \
      -not -path './.git/*' -not -path '*/__pycache__/*' \
      -printf '%P\n' | sort)
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
  echo "nothing to lint"
  exit 0
fi

# Unused imports and dead variables go first: the later checkers would
# only report what this can remove on its own.
autoflake --in-place --remove-all-unused-imports \
  --remove-unused-variables "${TARGETS[@]}"
autoflake_status=$?

isort --quiet "${TARGETS[@]}"
isort_status=$?

autopep8 --in-place --aggressive --aggressive "${TARGETS[@]}"
autopep8_status=$?

# Names from private work must not be used as worked examples. This has
# happened: a real task name reached a public repository as a docstring
# example and had to be purged from every commit after the fact. A grep
# is a poor substitute for judgement, but it is the only thing that runs
# every time and it costs nothing.
deny_status=0
leaked=""
if [ -n "$AGENTBUS_DENY" ]; then
  leaked=$(grep -rniE "$AGENTBUS_DENY" . \
    --exclude-dir=.git --exclude-dir=__pycache__ \
    --exclude=.agentbus-deny || true)
fi
if [ -n "$leaked" ]; then
  echo "refusing: private project names used as examples" >&2
  printf '%s\n' "$leaked" >&2
  echo "Use an invented name in examples, never a real one." >&2
  deny_status=1
fi

flake8 --docstring-convention=google \
  --per-file-ignores="__init__.py:D104" "${TARGETS[@]}"
flake_status=$?

pydocstyle --convention=google --match='(?!__init__).*\.py' "${TARGETS[@]}"
pydocstyle_status=$?

# R0402 is disabled because it pushes "import x.y as y", which is the
# opposite of the full-module imports this codebase uses.
pylint \
  --disable=R0402 \
  --good-names=i,j,k,ex,Run,_,revision,down_revision,branch_labels,depends_on \
  "${TARGETS[@]}"
pylint_status=$?

exit $((flake_status | pylint_status | pydocstyle_status \
  | isort_status | autopep8_status | autoflake_status | deny_status))
