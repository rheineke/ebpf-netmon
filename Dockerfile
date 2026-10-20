# Build on Debian trixie, the same release as the Pis, so the binary runs there.
FROM debian:trixie AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
        bpftool clang g++ libbpf-dev libelf-dev linux-libc-dev llvm make \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY Makefile ./
COPY src ./src
RUN make

FROM debian:trixie-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        libelf1t64 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/netmon /usr/local/bin/netmon
ENTRYPOINT ["netmon"]

# Generates known TCP and UDP traffic and checks netmon counted it.
FROM runtime AS test
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY tests ./tests
ENTRYPOINT ["python3", "tests/smoke_test.py"]
