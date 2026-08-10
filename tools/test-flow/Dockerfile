ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG BASE_IMAGE
ARG UV_SHA256
ARG UVX_SHA256
ARG UV_VERSION
ARG CLAUDE_CLI_SHA256
ARG CLAUDE_VERSION
ARG PYTHON_VERSION
ARG HATCHLING_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    UV_NO_PROGRESS=1 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PROJECT_ENVIRONMENT=/opt/venvs/xiaodao \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN sed -i \
      -e 's#http://archive.ubuntu.com/ubuntu/#http://mirrors.tuna.tsinghua.edu.cn/ubuntu/#' \
      -e 's#http://security.ubuntu.com/ubuntu/#http://mirrors.tuna.tsinghua.edu.cn/ubuntu/#' \
      /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends ca-certificates \
    && sed -i 's#http://mirrors.tuna.tsinghua.edu.cn/ubuntu/#https://mirrors.tuna.tsinghua.edu.cn/ubuntu/#' \
      /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
      ca-certificates curl git file xz-utils gpgv nodejs passwd util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uvcache uv /usr/local/bin/uv
COPY --from=uvcache uvx /usr/local/bin/uvx
RUN mkdir -p /opt/claude-code
COPY --from=claudecache package/cli.js package/package.json /opt/claude-code/
COPY --from=claudecache package/vendor /opt/claude-code/vendor
RUN echo "${UV_SHA256}  /usr/local/bin/uv" | sha256sum -c - \
    && echo "${UVX_SHA256}  /usr/local/bin/uvx" | sha256sum -c - \
    && echo "${CLAUDE_CLI_SHA256}  /opt/claude-code/cli.js" | sha256sum -c - \
    && test "$(node -p "require('/opt/claude-code/package.json').version")" = "${CLAUDE_VERSION}" \
    && printf '%s\n' '#!/bin/sh' 'exec node /opt/claude-code/cli.js "$@"' > /usr/local/bin/claude \
    && chmod 0755 /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/claude \
    && uv python install "${PYTHON_VERSION}" \
    && test "$(/usr/local/bin/claude --version)" = "${CLAUDE_VERSION} (Claude Code)"

WORKDIR /opt/e2e-lock
COPY pyproject.toml uv.lock ./
COPY tools/test-flow/logparse-requirements.txt ./
RUN mkdir -p /opt/venvs \
    && uv sync --frozen --all-groups --no-install-project --python "${PYTHON_VERSION}" \
    && uv pip install --python /opt/venvs/xiaodao/bin/python "hatchling==${HATCHLING_VERSION}" \
    && uv venv --python "${PYTHON_VERSION}" /opt/venvs/logparse \
    && uv pip install --python /opt/venvs/logparse/bin/python -r ./logparse-requirements.txt \
    && test "$(/opt/venvs/xiaodao/bin/python -c 'import importlib.metadata as m; print(m.version("hatchling"))')" = "${HATCHLING_VERSION}" \
    && uv pip check --python /opt/venvs/xiaodao/bin/python \
    && uv pip check --python /opt/venvs/logparse/bin/python \
    && touch /opt/e2e-lock/base-ready

LABEL problem-locator.e2e.base-digest="${BASE_IMAGE}" \
      problem-locator.e2e.python="${PYTHON_VERSION}" \
      problem-locator.e2e.uv="${UV_VERSION}" \
      problem-locator.e2e.hatchling="${HATCHLING_VERSION}" \
      problem-locator.e2e.claude="npm-${CLAUDE_VERSION}" \
      problem-locator.e2e.contains-secrets="false"

WORKDIR /
CMD ["sleep", "infinity"]
