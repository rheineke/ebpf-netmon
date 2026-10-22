#!/bin/sh
# Builds netmon natively on a Raspberry Pi (Raspberry Pi OS / Debian trixie,
# arm64) and runs the tests. Run it from a clone of this repo:
#
#   scripts/build-on-pi.sh            install packages, build, run all tests
#   scripts/build-on-pi.sh --no-deps  skip apt-get (packages already installed)
#   scripts/build-on-pi.sh --no-smoke skip the smoke test, which needs sudo
#
# The binary ends up in build/netmon. Run it with: sudo build/netmon
set -eu

deps=1
smoke=1
for arg in "$@"; do
	case "$arg" in
	--no-deps) deps=0 ;;
	--no-smoke) smoke=0 ;;
	*) sed -n '2,10s/^# \{0,1\}//p' "$0" >&2; exit 2 ;;
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
for opt in BPF_SYSCALL BPF_EVENTS KPROBES; do
	if [ -r /proc/config.gz ] && ! zgrep -q "^CONFIG_$opt=y" /proc/config.gz; then
		echo "kernel lacks CONFIG_$opt; netmon cannot run here" >&2
		exit 1
	fi
done

echo "== Building"
rm -rf build
make all test

if [ "$smoke" = 1 ]; then
	echo "== Smoke test (sudo)"
	sudo env PATH="$PWD/build:$PATH" python3 tests/smoke_test.py
fi

echo "== Done: build/netmon"
