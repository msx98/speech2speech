FROM localhost/cuda-rocm-vulkan-base:cuda13.4-rocm7.2.1

WORKDIR /app

RUN apt-get update && apt-get install -y python3-pip python3-dev python3-venv sox libsox-fmt-all libspeexdsp1 ffmpeg && rm -rf /var/lib/apt/lists/*

RUN dpkg -L sox
RUN command -v sox && sox --version

USER ubuntu

ENTRYPOINT ["/bin/bash"]
CMD ["/app/entrypoint.sh"]

