# Turns PDF and PowerPoint decks into page images. It has no network: it only sees a shared folder.
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-impress poppler-utils python3 python3-pip fonts-dejavu fonts-liberation fonts-noto-core \
 && pip3 install --break-system-packages python-pptx==1.0.2 \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 slides
COPY convert.py /convert.py
USER slides
CMD ["python3", "-u", "/convert.py"]
