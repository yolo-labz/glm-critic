FROM python:3.12-slim

# Sem etapa de build e sem dependências: o pacote é stdlib puro, então a imagem
# é o interpretador mais 200 KB de código. Um serviço que carrega uma chave de
# API não ganha nada importando um framework HTTP.
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-compile .

# O log de vereditos vive em volume montado em /data pelo Dokku; se ficar dentro
# da imagem ele some a cada deploy e o cache vira enfeite.
ENV CRITIC_LOG=/data/verdicts.jsonl
VOLUME ["/data"]

RUN useradd --system --uid 10001 critic && mkdir -p /data && chown -R critic /data
USER critic

EXPOSE 8080
# `serve` se recusa a subir sem SERVICE_TOKEN — falha alto em vez de expor o
# endpoint a quem alcançar a rede.
CMD ["glm-critic", "serve", "--host", "0.0.0.0", "--port", "8080"]
