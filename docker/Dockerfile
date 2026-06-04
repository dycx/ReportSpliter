# 多语言代码切片分析环境
# 包含所有需要的分析工具

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# 基础工具
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    wget \
    git \
    vim \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Java 环境 (用于 WALA)
RUN apt-get update && apt-get install -y \
    openjdk-17-jdk \
    maven \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

# Clang/LLVM 环境 (用于 SVF)
RUN apt-get update && apt-get install -y \
    clang-14 \
    llvm-14 \
    llvm-14-dev \
    llvm-14-tools \
    && rm -rf /var/lib/apt/lists/*

# 设置 Clang 默认版本
RUN update-alternatives --install /usr/bin/clang clang /usr/bin/clang-14 100 \
    && update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-14 100 \
    && update-alternatives --install /usr/bin/llvm-link llvm-link /usr/bin/llvm-link-14 100

# Python 分析工具
RUN pip3 install --no-cache-dir \
    tree-sitter \
    pyan3 \
    sqlglot \
    networkx \
    pygraphviz

# SVF (从源码编译)
# 注意: SVF 编译需要较长时间，建议预编译后 COPY 进来
# RUN git clone https://github.com/SVF-tools/SVF.git /opt/svf \
#     && cd /opt/svf && ./build.sh

# 工作目录
WORKDIR /data/project

# 挂载点
VOLUME ["/data/project", "/data/output"]

CMD ["/bin/bash"]
