ARG BASE_IMAGE=ubuntu@sha256:3131b4cc82a783df6c9df078f86e01819a13594b865c2cad47bd1bca2b7063bb
FROM ${BASE_IMAGE}

ARG UV_SHA256=da15297d6879b2cfbe5ea3cb03725c1613d51ba72892cc996468d871f0a532fb
ARG UVX_SHA256=31e409e837c16cbe9bdfd6534a1e2f6a774d937988027a4f0736ab52c7b6864d
ARG CLAUDE_CLI_SHA256=a9950ef6407fdc750bddb673852485500387e524a99d42385cb81e7d17128e01

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
    && test "$(node -p "require('/opt/claude-code/package.json').version")" = '2.1.89' \
    && printf '%s\n' '#!/bin/sh' 'exec node /opt/claude-code/cli.js "$@"' > /usr/local/bin/claude \
    && chmod 0755 /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/claude \
    && uv python install 3.12.13 \
    && test "$(/usr/local/bin/claude --version)" = '2.1.89 (Claude Code)'

WORKDIR /opt/e2e-lock
COPY pyproject.toml uv.lock ./
COPY tools/e2e/logparse-requirements.txt ./
RUN mkdir -p /opt/venvs \
    && uv sync --frozen --all-groups --no-install-project --python 3.12.13 \
    && uv venv --python 3.12.13 /opt/venvs/logparse \
    && uv pip install --python /opt/venvs/logparse/bin/python -r ./logparse-requirements.txt \
    && uv pip check --python /opt/venvs/xiaodao/bin/python \
    && uv pip check --python /opt/venvs/logparse/bin/python \
    && touch /opt/e2e-lock/base-ready

LABEL problem-locator.e2e.base-digest="sha256:3131b4cc82a783df6c9df078f86e01819a13594b865c2cad47bd1bca2b7063bb" \
      problem-locator.e2e.python="3.12.13" \
      problem-locator.e2e.uv="0.11.32" \
      problem-locator.e2e.claude="npm-2.1.89" \
      problem-locator.e2e.contains-secrets="false"

WORKDIR /
CMD ["sleep", "infinity"]
