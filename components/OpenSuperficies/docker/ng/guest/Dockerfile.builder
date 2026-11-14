FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      bc \
      bison \
      flex \
      libssl-dev \
      libelf-dev \
      libncurses-dev \
      kmod \
      cpio \
      e2fsprogs \
      curl \
      xz-utils \
      ca-certificates \
      jq \
      tar \
  && rm -rf /var/lib/apt/lists/*
