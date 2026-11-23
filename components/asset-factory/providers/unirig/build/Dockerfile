# Prepared only after the offline input gate succeeds. Never use a tag-only base.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY source.tar /tmp/source.tar
COPY requirements.lock /tmp/requirements.lock
COPY wheels/ /tmp/wheels/
RUN mkdir /opt/unirig && tar --no-same-owner --strip-components=1 -xf /tmp/source.tar -C /opt/unirig \
    && python3 -m pip install --no-index --require-hashes --find-links=/tmp/wheels -r /tmp/requirements.lock
WORKDIR /opt/unirig
# Weights are read-only runtime mounts, never image layers. No inference at build.
