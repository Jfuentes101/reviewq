# The review agent. Ephemeral: one container per review, --rm, no host writes.
#
# The uid matters. The credentials file is mounted mode 600 from the host, so the
# container's user has to BE the host user or it cannot read it — build with
# --build-arg UID=$(id -u) --build-arg GID=$(id -g). The base image already holds
# uid 1000 for `node`, hence the userdel.
#
# A project whose review command runs its own toolchain (rubocop, eslint, rspec)
# should build a child image FROM this one and point REVIEWQ_IMAGE at it. Keeping
# that out of here is what keeps this small enough to rebuild without thinking.
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git jq ripgrep less python3 \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

ARG UID=1000
ARG GID=1000
RUN userdel -r node 2>/dev/null || true; \
    getent group "$GID" >/dev/null || groupadd -g "$GID" reviewer; \
    useradd -u "$UID" -g "$GID" --create-home --shell /bin/bash reviewer; \
    mkdir -p /work /home/reviewer/.claude && chown -R "$UID:$GID" /work /home/reviewer

USER reviewer
WORKDIR /work
# your workspace is bind-mounted from the host, so its uid need not match ours and
# git refuses to read it without this
RUN git config --global --add safe.directory '*'

# No ENTRYPOINT on purpose: reviewq passes `--entrypoint bash` and the whole script
# on the command line, so the run recipe lives in one place instead of two.
