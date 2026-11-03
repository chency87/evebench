# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.11-trixie AS dev



ARG USERNAME=user
ARG USER_UID=1000
ARG USER_GID=1000


ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/home/user

ENV VIRTUAL_ENV=/opt/venv

ENV UV_TOOL_BIN_DIR=/home/user/.local/bin
ENV CARGO_HOME=/home/user/.cargo
ENV RUSTUP_HOME=/home/user/.rustup

ENV PATH=$CARGO_HOME/bin:$VIRTUAL_ENV/bin:$UV_TOOL_BIN_DIR:$PATH
ENV UV_PROJECT_ENVIRONMENT=$VIRTUAL_ENV

ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

RUN --mount=type=cache,target=/var/cache/apt/ \
    --mount=type=cache,target=/var/lib/apt/ \
    apt-get update && \
    apt-get install --no-install-recommends --yes \
      build-essential \
      ca-certificates \
      curl \
      git \
      libclang-dev \
      libz3-dev \
      graphviz \
      locales \
      maven \
      sqlite3 \
      ssh \
      sudo \
      vim \
      tmux \
      wget \
      z3 \
      cvc5 \
      unzip && \
    # Download and extract Java 17
    wget -qO /tmp/openjdk-17.tar.gz https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.10%2B7/OpenJDK17U-jdk_x64_linux_hotspot_17.0.10_7.tar.gz && \
    mkdir -p /opt/java/openjdk-17 && \
    tar -xzf /tmp/openjdk-17.tar.gz -C /opt/java/openjdk-17 --strip-components=1 && \
    rm /tmp/openjdk-17.tar.gz && \
    # Set up user
    groupadd --gid $USER_GID user && \
    useradd --create-home --no-log-init --gid $USER_GID --uid $USER_UID --shell /usr/bin/bash user && \
    mkdir -p /opt/venv && chown user:user /opt/venv && \
    echo 'user ALL=(root) NOPASSWD:ALL' > /etc/sudoers.d/user && chmod 0440 /etc/sudoers.d/user

ENV JAVA_HOME=/opt/java/openjdk-17
ENV JAVA19_HOME=/opt/java/openjdk-19
ENV PATH=${JAVA_HOME}/bin:${PATH}

# Install Java 19 for the QED parser; Java 17 remains the default.
RUN wget -qO /tmp/openjdk-19.tar.gz https://github.com/adoptium/temurin19-binaries/releases/download/jdk-19.0.2%2B7/OpenJDK19U-jdk_x64_linux_hotspot_19.0.2_7.tar.gz && \
    mkdir -p "${JAVA19_HOME}" && \
    tar -xzf /tmp/openjdk-19.tar.gz -C "${JAVA19_HOME}" --strip-components=1 && \
    rm /tmp/openjdk-19.tar.gz && \
    java -version && \
    "${JAVA19_HOME}/bin/java" -version && \
    JAVA_HOME="${JAVA19_HOME}" PATH="${JAVA19_HOME}/bin:${PATH}" mvn -version

# Install Gradle
RUN wget https://services.gradle.org/distributions/gradle-7.3.3-bin.zip -P /tmp && \
    unzip -d /opt/gradle /tmp/gradle-7.3.3-bin.zip && \
    rm /tmp/gradle-7.3.3-bin.zip

ENV GRADLE_HOME=/opt/gradle/gradle-7.3.3
ENV PATH=${GRADLE_HOME}/bin:${PATH}

RUN git config --system --add safe.directory '*'
RUN sh -c "$(curl -fsSL https://starship.rs/install.sh)" -- --yes
RUN printf 'export PATH="%s:$PATH"\n' "$VIRTUAL_ENV/bin:$UV_TOOL_BIN_DIR" > /etc/profile.d/devcontainer-path.sh

SHELL ["/bin/bash", "-c"]

USER user
RUN mkdir -p "$HOME/.local/bin" "$HOME/.local/share" "$CARGO_HOME" "$RUSTUP_HOME"

RUN uv venv "${VIRTUAL_ENV}"


ARG RUST_VERSION="nightly"

RUN --mount=type=cache,target=/home/user/.cargo/registry \
    --mount=type=cache,target=/home/user/.cargo/git \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
      --default-toolchain ${RUST_VERSION} \
      --profile default \
      --no-modify-path && \
    cargo --version && \
    rustc --version && \
    rustup component add rust-analyzer clippy rustfmt


ARG NODE_VERSION="24"
ARG NVM_VERSION="0.40.3"
ARG PNPM_VERSION="latest"
ENV NVM_DIR=/home/user/.nvm
ENV NVM_SYMLINK_CURRENT=true
ENV PATH="${NVM_DIR}/current/bin:${PATH}"

RUN mkdir -p "$NVM_DIR" \
    && curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v${NVM_VERSION}/install.sh | bash \
    && source "$NVM_DIR/nvm.sh" \
    && nvm install "$NODE_VERSION" \
    && nvm alias default "$NODE_VERSION" \
    && nvm use default \
    && corepack enable \
    && corepack prepare pnpm@${PNPM_VERSION} --activate



RUN mkdir -p "$HOME/.history" && \
    echo 'export HISTFILE=$HOME/.history/.bash_history' >> "$HOME/.bashrc" && \
    echo 'export HISTSIZE=10000'                        >> "$HOME/.bashrc" && \
    echo 'export HISTFILESIZE=20000'                    >> "$HOME/.bashrc" && \
    echo 'export HISTCONTROL=ignoredups:erasedups'      >> "$HOME/.bashrc" && \
    echo 'shopt -s histappend'                          >> "$HOME/.bashrc" && \
    echo 'PROMPT_COMMAND="history -a; history -c; history -r"' >> "$HOME/.bashrc" && \
    echo 'export PATH=$VIRTUAL_ENV/bin:$UV_TOOL_BIN_DIR:$PATH' >> "$HOME/.bashrc" && \
    echo '[[ $- == *i* ]] && bind "\e[A":history-search-backward' >> "$HOME/.bashrc" && \
    echo '[[ $- == *i* ]] && bind "\e[B":history-search-forward'  >> "$HOME/.bashrc" && \
    echo 'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi'  > "$HOME/.bash_profile" && \
    echo 'export NVM_DIR=/home/user/.nvm'                          >> "$HOME/.bashrc" && \
    echo 'export CARGO_HOME=/home/user/.cargo'                    >> "$HOME/.bashrc" && \
    echo 'export RUSTUP_HOME=/home/user/.rustup'                  >> "$HOME/.bashrc" && \
    echo 'export PATH=$CARGO_HOME/bin:$VIRTUAL_ENV/bin:$UV_TOOL_BIN_DIR:$NVM_DIR/current/bin:$PATH' >> "$HOME/.bashrc" && \
    echo '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"'          >> "$HOME/.bashrc" && \
    echo '[ -s "$NVM_DIR/bash_completion" ] && . "$NVM_DIR/bash_completion"' >> "$HOME/.bashrc"

SHELL ["/bin/bash", "-lc"]

USER root
RUN mkdir -p /workspaces/evebench && chown user:user /workspaces/evebench
USER user
