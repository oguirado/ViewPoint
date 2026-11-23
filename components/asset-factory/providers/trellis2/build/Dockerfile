# Both arguments must be digest-qualified images from build-inputs.json.
# Inputs are prepared offline by build.py; this file never fetches dependencies.
ARG BUILDER_IMAGE
ARG RUNTIME_IMAGE
ARG CONTEXT_SHA256
FROM --platform=linux/arm64 ${BUILDER_IMAGE} AS builder
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ARG CONTEXT_SHA256
COPY . /inputs/context/
WORKDIR /inputs/context
RUN printf '%s  context.sha256\n' "$CONTEXT_SHA256" | sha256sum -c - && sha256sum -c context.sha256 && \
    find . \( -type f -o -type l \) -print | LC_ALL=C sort > /tmp/context.paths && \
    cmp context.paths /tmp/context.paths && \
    find . -type l -print | LC_ALL=C sort > /tmp/context.links && cmp context.links /tmp/context.links && \
    while IFS= read -r link; do test "$(readlink "$link")" = /usr/include/eigen3 || exit 5; done < context.links
RUN dpkg -i debs/builder/libexpat1_*.deb && \
    dpkg -i debs/builder/libpython3.12-minimal_*.deb && \
    dpkg -i debs/builder/python3.12-minimal_*.deb && \
    dpkg -i debs/builder/python3-minimal_*.deb && \
    dpkg -i debs/builder/*.deb && test -z "$(dpkg --audit)"
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:/usr/local/cuda/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN python3 -m pip install --no-index --find-links=wheels --require-hashes -r requirements.lock
RUN cp -a sources /sources
# [TARGET] Bound native compiler fan-out inside the externally capped build.
ENV TORCH_CUDA_ARCH_LIST="12.0;12.1+PTX" FORCE_CUDA=1 BUILD_TARGET=cuda MAX_JOBS=4 CMAKE_BUILD_PARALLEL_LEVEL=4
RUN python3 -m pip install --no-index --no-deps --no-build-isolation /sources/cumesh
RUN TORCH_CUDA_ARCH_LIST=12.0 FLEXGEMM_TARGET_ARCH=120 python3 -m pip install --no-index --no-deps --no-build-isolation /sources/flexgemm
RUN python3 -m pip install --no-index --no-deps --no-build-isolation /sources/trellis2/o-voxel
RUN python3 -m pip check

FROM --platform=linux/arm64 ${RUNTIME_IMAGE} AS runtime
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ARG CONTEXT_SHA256
COPY . /inputs/context/
WORKDIR /inputs/context
RUN printf '%s  context.sha256\n' "$CONTEXT_SHA256" | sha256sum -c - && sha256sum -c context.sha256 && \
    find . \( -type f -o -type l \) -print | LC_ALL=C sort > /tmp/context.paths && \
    cmp context.paths /tmp/context.paths && \
    find . -type l -print | LC_ALL=C sort > /tmp/context.links && cmp context.links /tmp/context.links && \
    while IFS= read -r link; do test "$(readlink "$link")" = /usr/include/eigen3 || exit 5; done < context.links
RUN dpkg -i debs/runtime/libexpat1_*.deb && \
    dpkg -i debs/runtime/libpython3.12-minimal_*.deb && \
    dpkg -i debs/runtime/python3.12-minimal_*.deb && \
    dpkg -i debs/runtime/python3-minimal_*.deb && \
    dpkg -i debs/runtime/*.deb && test -z "$(dpkg --audit)"
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/local/cuda/bin/ptxas /usr/local/cuda/bin/ptxas
COPY --from=builder /sources/trellis2/trellis2 /opt/trellis2/trellis2
COPY build.py runtime_probe.py process_guard.py build-inputs.json THIRD_PARTY_NOTICES.txt /opt/trellis2/
ENV PATH=/opt/venv/bin:/usr/local/cuda/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENV PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 ATTN_BACKEND=sdpa SPARSE_ATTN_BACKEND=sdpa SPARSE_CONV_BACKEND=flex_gemm
ENV TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas
WORKDIR /opt/trellis2
# -I isolates the probe; it adds this declared source directory explicitly.
# Imports/linking are a prerequisite, never geometry or appearance acceptance.
ENTRYPOINT ["python3", "-I", "-B", "/opt/trellis2/runtime_probe.py"]
