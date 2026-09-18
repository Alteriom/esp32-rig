#!/usr/bin/env bash
#
# Run pytest, retrying ONLY when the interpreter itself crashes on a fatal
# signal (SIGSEGV and friends) -- never when a test fails.
#
# Why this exists
# ---------------
# The HAL unit-test job segfaults on Python 3.12 roughly one run in seven, in
# a burst that comes and goes with the runner's memory/allocator state. The
# crash lands in ordinary pure-Python code (usually yaml.safe_load, reached
# through admin_cli's config commands), which cannot corrupt memory on its own:
# the farm's Python has no ctypes, struct-buffer, or threading that could do
# it. It is a CPython-level heap corruption (a freed callable is reached
# through a NULL vectorcall pointer) that the large test collection exposes by
# how it lays out the heap. It is not a bug in a test and not in the library.
# The full investigation, with backtraces and the reproduction recipe, is in
# docs/hal-test-segfault-investigation.md.
#
# Because the crash is in the interpreter and not in a test, a plain re-run
# passes. This wrapper makes that automatic for signal-class exits so a known
# interpreter flake does not turn the gate red, while:
#   * keeping the crash visible -- every crash is announced as a CI warning and
#     its faulthandler dump is kept for upload, so a real regression is never
#     silently swallowed;
#   * never retrying a test failure -- any exit below 128 (pass, fail, usage,
#     no-tests-collected) is returned verbatim on the first run;
#   * giving up loudly if the interpreter crashes on every attempt, so a crash
#     that has become deterministic still fails the job.
#
# PYTHONFAULTHANDLER makes CPython print the C-and-Python traceback of the
# crashing thread on a fatal signal -- the evidence that was missing from the
# original CI failures, which ended with only "Extension modules: ...".
set -u

ATTEMPTS="${PYTEST_GUARD_ATTEMPTS:-3}"
CRASH_DIR="${PYTEST_CRASH_DIR:-pytest-crash-logs}"
mkdir -p "$CRASH_DIR"

rc=0
for attempt in $(seq 1 "$ATTEMPTS"); do
  log="$CRASH_DIR/attempt-${attempt}.log"
  PYTHONFAULTHANDLER=1 python -X faulthandler -m pytest "$@" 2>&1 | tee "$log"
  rc="${PIPESTATUS[0]}"

  if [ "$rc" -lt 128 ]; then
    # Normal pytest outcome (0 pass, 1 fail, 2 interrupted, 4 usage,
    # 5 no tests). Not a crash -- return it exactly, retry nothing.
    rm -f "$log"
    exit "$rc"
  fi

  signal=$((rc - 128))
  echo "::warning title=pytest interpreter crash::pytest exited on fatal signal ${signal} (exit ${rc}) on attempt ${attempt}/${ATTEMPTS} for [$*]. Known intermittent CPython-level crash; retrying. Dump: ${log}. See docs/hal-test-segfault-investigation.md"
done

echo "::error title=pytest crashed every attempt::pytest crashed on a fatal signal ${ATTEMPTS} times in a row for [$*]; this is not being masked. Dumps are in ${CRASH_DIR}."
exit "$rc"
