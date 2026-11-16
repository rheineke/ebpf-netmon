#!/bin/bash
# Builds netmon natively on a Raspberry Pi (Raspberry Pi OS / Debian trixie,
# arm64) and runs the tests. Run it from a clone of this repo:
#
#   scripts/build_on_pi.sh            install packages, build, run all tests
#   scripts/build_on_pi.sh --no-deps  skip apt-get (packages already installed)
#   scripts/build_on_pi.sh --no-smoke skip the smoke test, which needs sudo
#
# The binary ends up in build/netmon. Run it with: sudo build/netmon
set -eu

deps=1
smoke=1
for arg in "$@"; do
  case "$arg" in
    --no-deps) deps=0 ;;
    --no-smoke) smoke=0 ;;
    *)
      sed -n '2,10s/^# \{0,1\}//p' "$0" >&2
      exit 2
      ;;
  esac
done

cd "$(dirname "$0")/.."

if [ "$deps" = 1 ]; then
  echo "== Installing build packages"
  sudo apt-get update
  # The same packages as the Dockerfile, plus python3 for the smoke test.
  sudo apt-get install -y --no-install-recommends \
    bpftool clang g++ libbpf-dev libelf-dev linux-libc-dev llvm \
    make python3 zlib1g-dev
fi

echo "== Checking the kernel"
uname -r
# Raspberry Pi kernels publish their config only once the module is loaded.
[ -r /proc/config.gz ] || sudo modprobe -q configs 2>/dev/null || true
if [ -r /proc/config.gz ]; then
  for opt in BPF_SYSCALL BPF_EVENTS KPROBES KPROBE_EVENTS EVENT_TRACING; do
    if ! zgrep -q "^CONFIG_$opt=y" /proc/config.gz; then
      echo "kernel lacks CONFIG_$opt; netmon cannot run here" >&2
      exit 1
    fi
  done
else
  echo "no /proc/config.gz; skipped the kernel config check"
fi

echo "== Building"
rm -rf build
make all test

if [ "$smoke" = 1 ]; then
  echo "== Smoke test (sudo)"
  sudo env PATH="$PWD/build:$PATH" python3 tests/smoke_test.py
fi

echo "== Done: build/netmon"
