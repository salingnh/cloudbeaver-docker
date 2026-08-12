# syntax=docker/dockerfile:1.7

ARG JAVA_IMAGE=eclipse-temurin:21-jdk-jammy
ARG BASE_JAVA_TAG=stable

FROM ${JAVA_IMAGE} AS builder

ARG CLOUDBEAVER_REF=devel
ARG DBEAVER_REF=devel
ARG DBEAVER_COMMON_REF=devel
ARG DBEAVER_JDBC_LIBSQL_REF=devel
ARG AVATICA_VERSION=1.28.0
ARG NODE_VERSION=22.15.0
ARG MAVEN_VERSION=3.9.16

ENV DEBIAN_FRONTEND=noninteractive
ENV MAVEN_HOME=/opt/maven
ENV NODE_HOME=/opt/node
ENV PATH=/opt/maven/bin:/opt/node/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       curl \
       git \
       python3 \
       xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install the toolchain versions documented by current CloudBeaver build docs.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) node_arch='x64' ;; \
      arm64) node_arch='arm64' ;; \
      *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" -o /tmp/node.tar.xz; \
    mkdir -p /opt/node; \
    tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1; \
    rm /tmp/node.tar.xz; \
    curl -fsSL "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" -o /tmp/maven.tar.gz; \
    mkdir -p /opt/maven; \
    tar -xzf /tmp/maven.tar.gz -C /opt/maven --strip-components=1; \
    rm /tmp/maven.tar.gz; \
    npm install -g corepack; \
    corepack enable; \
    java -version; \
    mvn -version; \
    node --version; \
    npm --version; \
    yarn --version || true

WORKDIR /work

# Fetch exact refs into the sibling layout expected by cloudbeaver/deploy/build-backend.sh.
RUN set -eux; \
    clone_ref() { \
      repo="$1"; dest="$2"; ref="$3"; \
      git clone --filter=blob:none --no-checkout "$repo" "$dest"; \
      git -C "$dest" fetch --depth 1 origin "$ref"; \
      git -C "$dest" checkout --detach FETCH_HEAD; \
    }; \
    clone_ref https://github.com/dbeaver/cloudbeaver.git cloudbeaver "$CLOUDBEAVER_REF"; \
    clone_ref https://github.com/dbeaver/dbeaver.git dbeaver "$DBEAVER_REF"; \
    clone_ref https://github.com/dbeaver/dbeaver-common.git dbeaver-common "$DBEAVER_COMMON_REF"; \
    clone_ref https://github.com/dbeaver/dbeaver-jdbc-libsql.git dbeaver-jdbc-libsql "$DBEAVER_JDBC_LIBSQL_REF"

# DBeaver devel currently declares Avatica as RELEASE[1.42.0], but Apache
# Avatica has its own release line (1.28.0 is the current release). 1.42.0 is
# a Calcite release and does not exist for avatica-core, so patch this upstream
# definition before generating the CloudBeaver driver pack. Keep it configurable
# so the workaround can be removed or changed when upstream fixes the definition.
RUN set -eux; \
    plugin=/work/dbeaver/plugins/org.jkiss.dbeaver.ext.generic/plugin.xml; \
    if grep -Fq 'org.apache.calcite.avatica:avatica-core:RELEASE[1.42.0]' "$plugin"; then \
      sed -i "s#org.apache.calcite.avatica:avatica-core:RELEASE\[1\.42\.0\]#org.apache.calcite.avatica:avatica-core:RELEASE[${AVATICA_VERSION}]#g" "$plugin"; \
    fi; \
    grep -F 'org.apache.calcite.avatica:avatica-core:' "$plugin"

COPY config/driver-policy.json /tooling/driver-policy.json
COPY scripts/generate_full_driver_pack.py /tooling/generate_full_driver_pack.py

RUN python3 /tooling/generate_full_driver_pack.py \
      --cloudbeaver /work/cloudbeaver \
      --dbeaver /work/dbeaver \
      --policy /tooling/driver-policy.json \
      --report /tmp/full-drivers-report.json

# CloudBeaver's build script builds the DBeaver platform, backend and frontend.
RUN --mount=type=cache,target=/root/.m2 \
    --mount=type=cache,target=/root/.yarn \
    cd /work/cloudbeaver/deploy \
    && ./build.sh \
    && test -x ./cloudbeaver/run-cloudbeaver-server.sh \
    && test -d ./cloudbeaver/drivers


FROM dbeaver/base-java:${BASE_JAVA_TAG} AS runtime

LABEL org.opencontainers.image.title="CloudBeaver Community Full JDBC"
LABEL org.opencontainers.image.description="CloudBeaver CE with additional Maven-backed DBeaver Community JDBC drivers pre-bundled"
LABEL org.opencontainers.image.source="https://github.com/salingnh/cloudbeaver-docker"

ENV DBEAVER_GID=8978
ENV DBEAVER_UID=8978

RUN groupadd -g ${DBEAVER_GID} dbeaver \
    && useradd -g ${DBEAVER_GID} -m -u ${DBEAVER_UID} -s /bin/bash dbeaver

COPY --from=builder /work/cloudbeaver/deploy/cloudbeaver /opt/cloudbeaver
COPY --from=builder /tmp/full-drivers-report.json /opt/cloudbeaver/full-drivers-report.json
COPY config/driver-policy.json /opt/cloudbeaver/full-driver-pack-policy.json

EXPOSE 8978
WORKDIR /opt/cloudbeaver

RUN find /opt/cloudbeaver -type d -exec chmod 775 {} \; \
    && chmod +x ./run-cloudbeaver-server.sh ./launch-product.sh

ENTRYPOINT ["./launch-product.sh"]
