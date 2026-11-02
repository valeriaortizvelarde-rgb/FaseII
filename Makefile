.PHONY: install setup-opensearch setup-ollama setup-localstack build-lambda setup-lambda ingest run test clean

CONTAINER_NAME := busrag-opensearch
OPENSEARCH_IMAGE := docker.io/opensearchproject/opensearch:2.11.1
OPENSEARCH_PORT := 9200
OLLAMA_EMBEDDING_MODEL := all-minilm
OLLAMA_CHAT_MODEL := llama3.2
LOCALSTACK_CONTAINER := busrag-localstack
LOCALSTACK_IMAGE := docker.io/localstack/localstack:4.0.0
LAMBDA_BUILD_IMAGE ?= localhost/busrag-lambda-builder:latest

install:
	poetry install

setup-ollama:
	@if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then \
	    if command -v ollama >/dev/null 2>&1; then \
	        echo "Arrancando Ollama..."; \
	        ollama serve >/tmp/ollama.log 2>&1 & \
	        sleep 3; \
	    else \
	        echo "La API de Ollama no responde y no se encontró el comando ollama."; \
	        exit 1; \
	    fi; \
	else \
	    echo "Ollama ya está activo."; \
	fi
	@echo "Asegurando modelo de embeddings $(OLLAMA_EMBEDDING_MODEL)..."
	@curl -fsS -X POST http://localhost:11434/api/pull \
		-H 'Content-Type: application/json' \
		-d '{"name":"$(OLLAMA_EMBEDDING_MODEL)","stream":false}' >/dev/null
	@echo "Asegurando modelo de chat $(OLLAMA_CHAT_MODEL)..."
	@curl -fsS -X POST http://localhost:11434/api/pull \
		-H 'Content-Type: application/json' \
		-d '{"name":"$(OLLAMA_CHAT_MODEL)","stream":false}' >/dev/null

setup-opensearch:
	@if podman ps --format '{{.Names}}' | grep -q '^$(CONTAINER_NAME)$$'; then \
		echo "OpenSearch ya está corriendo en el contenedor '$(CONTAINER_NAME)'."; \
	elif podman container exists $(CONTAINER_NAME) 2>/dev/null; then \
		echo "Contenedor '$(CONTAINER_NAME)' existe pero está detenido. Iniciando..."; \
		podman start $(CONTAINER_NAME); \
	else \
		echo "Creando y arrancando contenedor de OpenSearch '$(CONTAINER_NAME)'..."; \
		podman run -d --name $(CONTAINER_NAME) \
			-p $(OPENSEARCH_PORT):9200 -p 9600:9600 \
			-e "discovery.type=single-node" \
			-e "DISABLE_SECURITY_PLUGIN=true" \
			-e "OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m" \
			$(OPENSEARCH_IMAGE); \
	fi
	@echo "Esperando a que OpenSearch responda en el puerto $(OPENSEARCH_PORT)..."
	@for i in $$(seq 1 30); do \
		if curl -s -o /dev/null "http://localhost:$(OPENSEARCH_PORT)"; then \
			echo "OpenSearch disponible."; \
			exit 0; \
		fi; \
		sleep 2; \
	done; \
	echo "Advertencia: OpenSearch no respondió a tiempo, revisa 'podman logs $(CONTAINER_NAME)'."

setup-localstack:
	@podman network exists busrag || podman network create busrag
	@if podman container exists $(LOCALSTACK_CONTAINER) 2>/dev/null && \
		! podman inspect $(LOCALSTACK_CONTAINER) --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -q '^SERVICES=s3,lambda,iam,logs$$'; then \
		echo "Actualizando LocalStack para Lambda; se conserva el volumen de documentos."; \
		podman stop $(LOCALSTACK_CONTAINER) && podman rm $(LOCALSTACK_CONTAINER); \
	fi
	@if podman ps --format '{{.Names}}' | grep -q '^$(LOCALSTACK_CONTAINER)$$'; then \
		echo "LocalStack ya esta activo."; \
	elif podman container exists $(LOCALSTACK_CONTAINER) 2>/dev/null; then \
		podman start $(LOCALSTACK_CONTAINER); \
	else \
		socket="$$(podman info --format '{{.Host.RemoteSocket.Path}}')"; \
		socket="$${socket#unix://}"; \
		podman run -d --name $(LOCALSTACK_CONTAINER) \
			-p 127.0.0.1:4566:4566 \
			--network busrag --network-alias busrag-localstack \
			-e SERVICES=s3,lambda,iam,logs -e PERSISTENCE=1 \
			-e DOCKER_HOST=unix:///var/run/docker.sock \
			-e LAMBDA_DOCKER_NETWORK=busrag \
			-e LOCALSTACK_HOST=busrag-localstack:4566 \
			--security-opt label=disable \
			-v "$$socket:/var/run/docker.sock" \
			-v busrag-localstack-data:/var/lib/localstack \
			--health-cmd='awslocal s3api list-buckets >/dev/null 2>&1' \
			--health-interval=2s --health-retries=30 --health-start-period=5s \
			$(LOCALSTACK_IMAGE); \
	fi
	@podman wait --condition=healthy --condition=unhealthy $(LOCALSTACK_CONTAINER) >/dev/null
	@podman healthcheck run $(LOCALSTACK_CONTAINER)
	PYTHONPATH=src poetry run python -m infrastructure.genai.persistence.s3_client

build-lambda:
	podman build -t $(LAMBDA_BUILD_IMAGE) -f resources/lambda/Containerfile .
	@mkdir -p .build
	@container="$$(podman create $(LAMBDA_BUILD_IMAGE))"; \
	podman cp "$$container:/tmp/ingest-lambda.zip" .build/ingest-lambda.zip; \
	result=$$?; podman rm "$$container" >/dev/null; exit $$result

setup-lambda: setup-localstack build-lambda
	@architecture="$$(podman info --format '{{.Host.Arch}}')"; \
	if [ "$$architecture" = "amd64" ]; then architecture=x86_64; fi; \
	LAMBDA_ARCHITECTURE="$$architecture" PYTHONPATH=src poetry run python -m infrastructure.entrypoint.deploy_lambda

ingest: setup-opensearch setup-ollama setup-localstack
	PYTHONPATH=src poetry run python -m application.ingest

run: setup-ollama setup-lambda
	PYTHONPATH=src poetry run uvicorn infrastructure.entrypoint.fastapi_app:app --reload --host 0.0.0.0 --port 8000

test:
	@echo "Levantando la API en background para pruebas de integración..."
	@PYTHONPATH=src poetry run uvicorn infrastructure.entrypoint.fastapi_app:app --host 0.0.0.0 --port 8000 & echo $$! > .uvicorn.pid
	@sleep 4
	@echo "\n== GET /health ==" && curl -sf http://localhost:8000/health && echo
	@echo "\n== POST /ask ==" && curl -sf -X POST http://localhost:8000/ask \
		-H "Content-Type: application/json" \
		-d '{"message": "¿Qué tablas existen relacionadas con saldos mensuales?"}' && echo
	@kill $$(cat .uvicorn.pid) 2>/dev/null || true
	@rm -f .uvicorn.pid

clean:
	podman rm -f $(CONTAINER_NAME) 2>/dev/null || true
