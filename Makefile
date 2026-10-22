# Builds build/netmon: the BPF object is embedded via a bpftool skeleton.
ARCH := $(shell uname -m | sed -e 's/x86_64/x86/' -e 's/aarch64/arm64/')
MULTIARCH := $(shell $(CC) -print-multiarch)
CLANG ?= clang
# Debian installs bpftool in /usr/sbin, which is not on a normal user's PATH.
BPFTOOL ?= $(or $(shell command -v bpftool),/usr/sbin/bpftool)
CXXFLAGS ?= -O2 -g -Wall -Wextra -Werror
BUILD := build

.PHONY: all test tidy clean
all: $(BUILD)/netmon

$(BUILD):
	mkdir -p $@

$(BUILD)/netmon.bpf.o: src/netmon.bpf.c src/netmon.h | $(BUILD)
	$(CLANG) -g -O2 -Wall -Werror -target bpf -D__TARGET_ARCH_$(ARCH) \
		-I/usr/include/$(MULTIARCH) -c $< -o $@

$(BUILD)/netmon.skel.h: $(BUILD)/netmon.bpf.o
	$(BPFTOOL) gen skeleton $< > $@.tmp && mv $@.tmp $@

# libbpf is linked statically so the binary only needs libelf and zlib.
$(BUILD)/netmon: src/netmon.cpp src/netmon.h src/priority_sample.h \
		$(BUILD)/netmon.skel.h
	$(CXX) -std=c++20 $(CXXFLAGS) -I$(BUILD) -Isrc $< -o $@ \
		-Wl,-Bstatic -lbpf -Wl,-Bdynamic -lelf -lz

# Unit tests for the user-space code; no BPF or root needed.
$(BUILD)/priority_sample_test: tests/priority_sample_test.cpp \
		src/priority_sample.h | $(BUILD)
	$(CXX) -std=c++20 $(CXXFLAGS) -Isrc $< -o $@

test: $(BUILD)/priority_sample_test
	$<

# Lints the C++; the BPF C is checked by clang -Wall -Werror above.
tidy: $(BUILD)/netmon.skel.h
	clang-tidy --quiet --header-filter="src/.*" --exclude-header-filter="build/.*" src/netmon.cpp tests/priority_sample_test.cpp -- \
		-std=c++20 -I$(BUILD) -Isrc

clean:
	rm -rf $(BUILD)
