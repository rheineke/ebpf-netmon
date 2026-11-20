# Builds build/netmon: the BPF object is embedded via a bpftool skeleton.
ARCH := $(shell uname -m | sed -e 's/x86_64/x86/' -e 's/aarch64/arm64/')
MULTIARCH := $(shell $(CC) -print-multiarch)
CLANG ?= clang
# Debian installs bpftool in /usr/sbin, which is not on a normal user's PATH.
BPFTOOL ?= $(or $(shell command -v bpftool),/usr/sbin/bpftool)
# Hardened: netmon runs as root and reads names any process can set.
HARDEN := -D_FORTIFY_SOURCE=3 -fstack-protector-strong -fstack-clash-protection \
	-Wl,-z,relro,-z,now
CXXFLAGS ?= -O2 -g -Wall -Wextra -Werror $(HARDEN)
BUILD := build

.PHONY: all test tidy sanitize clean
all: $(BUILD)/netmon

$(BUILD):
	mkdir -p $@

# -mcpu=v3 for atomics that return the old value (kernel 5.12+).
$(BUILD)/netmon.bpf.o: src/netmon.bpf.c src/netmon.h | $(BUILD)
	$(CLANG) -g -O2 -Wall -Werror -target bpf -mcpu=v3 \
		-D__TARGET_ARCH_$(ARCH) \
		-I/usr/include/$(MULTIARCH) -c $< -o $@

$(BUILD)/netmon.skel.h: $(BUILD)/netmon.bpf.o
	$(BPFTOOL) gen skeleton $< > $@.tmp && mv $@.tmp $@

# libbpf is linked statically so the binary only needs libelf and zlib.
# (Debian's static libelf can't be linked: it lacks libeu.)
LDLIBS ?= -Wl,-Bstatic -lbpf -Wl,-Bdynamic -lelf -lz
HEADERS := $(wildcard src/*.h)
$(BUILD)/netmon: src/netmon.cpp $(HEADERS) $(BUILD)/netmon.skel.h
	$(CXX) -std=c++20 $(CXXFLAGS) -I$(BUILD) -Isrc $< -o $@ $(LDLIBS)

# Unit tests for the user-space code; no BPF or root needed.
UNIT_TESTS := $(patsubst tests/%.cpp,$(BUILD)/%,$(wildcard tests/*_test.cpp))
$(BUILD)/%_test: tests/%_test.cpp $(HEADERS) | $(BUILD)
	$(CXX) -std=c++20 $(CXXFLAGS) -Isrc $< -o $@

test: $(UNIT_TESTS)
	set -e; for t in $^; do $$t; done

# Lints the C++; the BPF C is checked by clang -Wall -Werror above.
tidy: $(BUILD)/netmon.skel.h
	clang-tidy --quiet --header-filter="src/.*" --exclude-header-filter="build/.*" src/netmon.cpp tests/*_test.cpp -- \
		-std=c++20 -I$(BUILD) -Isrc

# The same build with ASan and UBSan in build/asan; any report is fatal.
# Runs the unit tests; the Docker sanitize stage also runs the smoke test.
SANITIZE := -fsanitize=address,undefined -fno-sanitize-recover=all \
	-fno-omit-frame-pointer -U_FORTIFY_SOURCE
sanitize:
	$(MAKE) BUILD=$(BUILD)/asan CXXFLAGS="$(CXXFLAGS) $(SANITIZE)" all test

clean:
	rm -rf $(BUILD)
