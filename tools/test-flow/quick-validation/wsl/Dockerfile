FROM ubuntu@sha256:79676deb51ebb02885b0b9d33788e78a37cf1045ad79d1bb04c6a222c3556b3d

ARG NODE_VERSION=v24.16.0
ARG NODE_ARCHIVE_SHA256=d804845d34eddc21dc1092b519d643ef40b1f58ec5dec5c22b1f4bd8fabde6c9
ARG CODEX_VERSION=0.149.1
ARG CODEX_SHA256=73dc5888888f411c1f0fa7b81d866e721dcc86b527ce8e3b2cf4708661e823ba
ARG CODEX_CODE_MODE_HOST_SHA256=48f3a0d48033039cc7caccd209edb0ee350b81f82ca851a7b129e146e4bec6fb
ARG CLAUDE_VERSION=2.1.89
ARG CLAUDE_CLI_SHA256=a9950ef6407fdc750bddb673852485500387e524a99d42385cb81e7d17128e01
ARG CLAUDE_TARBALL_SHA256=680e35001b24b604f58958e3a324bb758be3c069c0a3f89585156256f17a9c87
ARG CLAUDE_PACKAGE_MANIFEST_SHA256=2cc59f27f893cbfc4dce4d8da7ea4a088d3721afeeed5e3e3438a0fcef92d109
ARG CLAUDE_PACKAGE_TREE_DIGEST=d566d797d3f1438896abfe89c24b7100740f590ba48af79581874467a520f607
ARG UV_SHA256=da15297d6879b2cfbe5ea3cb03725c1613d51ba72892cc996468d871f0a532fb
ARG UVX_SHA256=31e409e837c16cbe9bdfd6534a1e2f6a774d937988027a4f0736ab52c7b6864d
ARG PYTHON_VERSION=3.12.13
ARG HATCHLING_VERSION=1.28.0
ARG LOGPARSE_COMMIT=a233b500d9c99e6815d1ffd82cb4ca55bbfe657a

ENV DEBIAN_FRONTEND=noninteractive \
    UV_NO_PROGRESS=1 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PROJECT_ENVIRONMENT=/opt/venvs/xiaodao \
    PATH=/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
      bash bubblewrap ca-certificates coreutils curl file git netcat-openbsd openssl xz-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=nodecache node-v24.16.0-linux-x64.tar.xz /tmp/node.tar.xz
RUN echo "${NODE_ARCHIVE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && mkdir -p /opt/node \
    && tar -xJf /tmp/node.tar.xz --strip-components=1 -C /opt/node \
    && test "$(node --version)" = "${NODE_VERSION}" \
    && rm /tmp/node.tar.xz

COPY --from=codexcache codex codex-code-mode-host /opt/codex/bin/
RUN echo "${CODEX_SHA256}  /opt/codex/bin/codex" | sha256sum -c - \
    && echo "${CODEX_CODE_MODE_HOST_SHA256}  /opt/codex/bin/codex-code-mode-host" | sha256sum -c - \
    && install -m 0755 /opt/codex/bin/codex /usr/bin/codex \
    && install -m 0755 /opt/codex/bin/codex-code-mode-host /usr/bin/codex-code-mode-host \
    && install -m 0755 /opt/codex/bin/codex /usr/bin/codex-linux-sandbox \
    && test "$(/usr/bin/codex --version)" = "codex-cli ${CODEX_VERSION}"

COPY --from=claudecache . /opt/claude-cache/
RUN echo "${CLAUDE_CLI_SHA256}  /opt/claude-cache/package/cli.js" | sha256sum -c - \
    && echo "${CLAUDE_TARBALL_SHA256}  /opt/claude-cache/claude-code-${CLAUDE_VERSION}.tgz" | sha256sum -c - \
    && echo "${CLAUDE_PACKAGE_MANIFEST_SHA256}  /opt/claude-cache/package/package.json" | sha256sum -c - \
    && node -e "const s=require('/opt/claude-cache/cache-seal.json'); if(s.package_tree_digest!=='${CLAUDE_PACKAGE_TREE_DIGEST}') process.exit(1)"

COPY --from=uvcache uv /usr/local/bin/uv
COPY --from=uvcache uvx /usr/local/bin/uvx
RUN echo "${UV_SHA256}  /usr/local/bin/uv" | sha256sum -c - \
    && echo "${UVX_SHA256}  /usr/local/bin/uvx" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/uv /usr/local/bin/uvx \
    && uv python install "${PYTHON_VERSION}"

WORKDIR /opt/xiaodao
COPY pyproject.toml uv.lock ./
COPY src/problem_locator ./src/problem_locator
COPY tools/test-flow/logparse-requirements.txt ./
RUN mkdir -p /opt/venvs \
    && uv venv --python "${PYTHON_VERSION}" /opt/venvs/xiaodao \
    && VIRTUAL_ENV=/opt/venvs/xiaodao uv sync --frozen --all-groups --active \
    && uv pip install --python /opt/venvs/xiaodao/bin/python "hatchling==${HATCHLING_VERSION}" \
    && uv venv --python "${PYTHON_VERSION}" /opt/venvs/logparse \
    && uv pip install --python /opt/venvs/logparse/bin/python -r ./logparse-requirements.txt \
    && /opt/venvs/xiaodao/bin/python -I -c "import problem_locator" \
    && ln -s /opt/venvs/xiaodao/bin/python /usr/bin/python3 \
    && test "$(/usr/bin/python3 --version)" = "Python ${PYTHON_VERSION}"

COPY --from=logparse . /opt/logparse
RUN test "$(git -C /opt/logparse rev-parse HEAD)" = "${LOGPARSE_COMMIT}" \
    && test -z "$(git -C /opt/logparse status --porcelain)" \
    && mv /opt/venvs/logparse /opt/logparse/.venv \
    && test "$(/opt/logparse/.venv/bin/python --version)" = "Python ${PYTHON_VERSION}" \
    && /opt/logparse/.venv/bin/python -I -c "import sys; assert sys.prefix == '/opt/logparse/.venv'" \
    && mv /usr/bin/stat /usr/bin/stat.gnu \
    && printf '%s\n' '#!/bin/sh' \
      'if [ "$#" -eq 3 ] && [ "$1" = "-f" ] && [ "$2" = "%z" ]; then exec /usr/bin/stat.gnu -c %s -- "$3"; fi' \
      'exec /usr/bin/stat.gnu "$@"' > /usr/bin/stat \
    && chmod 0755 /usr/bin/stat \
    && printf '%s\n' "${LOGPARSE_COMMIT}" > /opt/logparse-commit \
    && test "$(/usr/bin/stat -f %z /opt/logparse-commit)" = "41" \
    && mkdir -p -m 1777 /private/tmp

LABEL problem-locator.quick.container="ubuntu22.04-central-v1" \
      problem-locator.quick.codex="0.149.1" \
      problem-locator.quick.claude="2.1.89" \
      problem-locator.quick.contains-secrets="false"

USER 0:0
WORKDIR /workspace
CMD ["node", "--version"]
