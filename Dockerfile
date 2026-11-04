FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      g++ nginx gettext-base && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# build.sh expects core/core.cpp; tolerate a flat upload layout
RUN mkdir -p core && [ -f core/core.cpp ] || mv core.cpp core/core.cpp
RUN bash build.sh
RUN chmod +x start.sh
CMD ["./start.sh"]
