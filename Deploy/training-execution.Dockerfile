ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BULLET_REPOSITORY=https://github.com/jw1912/bullet
ARG BULLET_COMMIT=629ee50000b2afb7b3337595401c830d3b1e0f42
ENV CARGO_HOME=/opt/cargo
RUN git -c init.defaultBranch=main init /opt/bullet && git -C /opt/bullet remote add origin ${BULLET_REPOSITORY} && git -C /opt/bullet fetch --depth 1 origin ${BULLET_COMMIT} && git -C /opt/bullet checkout --detach FETCH_HEAD
RUN cargo fetch --locked --manifest-path /opt/bullet/Cargo.toml && chmod -R a+rX /opt/cargo
RUN rustc --version > /opt/toolchain-version && cargo --version >> /opt/toolchain-version
ENV CARGO_NET_OFFLINE=true
