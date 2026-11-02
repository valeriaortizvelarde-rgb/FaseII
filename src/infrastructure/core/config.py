"""Configuración centralizada de la aplicación, cargada desde variables de entorno (.env)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Proveedor LLM: "azure" (Azure OpenAI) u "ollama" (modelos locales) ---
    llm_provider: str = "ollama"

    # --- Azure OpenAI / Ollama (mismo set de variables, reinterpretadas según llm_provider) ---
    # Azure: https://<recurso>.openai.azure.com | Ollama: http://localhost:11434/v1
    openai_endpoint: str = "http://localhost:11434/v1"
    # Azure: api key real | Ollama: no se valida, cualquier valor sirve
    openai_api_key: str = "ollama"
    # Azure: nombre del deployment | Ollama: nombre del modelo de embeddings (p.ej. nomic-embed-text)
    openai_embedding_model: str = "all-minilm"
    # Sólo aplica a Azure OpenAI; se ignora con Ollama
    openai_api_version: str = ""
    # Azure: nombre del deployment | Ollama: nombre del modelo de chat (p.ej. llama3.1)
    openai_chat_model: str = "llama3.2"

    embedding_endpoint: str = "http://localhost:11434/v1"
    embedding_api_key: str = "ollama"

    # --- OpenSearch ---
    opensearch_host: str = "localhost"
    opensearch_port: int = 9200
    opensearch_user: str = "admin"
    opensearch_password: str = ""
    opensearch_use_ssl: bool = False
    opensearch_verify_certs: bool = False
    opensearch_index: str = "fast_example_minilm"

    s3_endpoint_url: str = "http://localhost:4566"
    s3_region: str = "us-east-1"
    s3_access_key_id: str = "test"
    s3_secret_access_key: str = "test"
    s3_bucket: str = "rag-documents"

    lambda_function_name: str = "busrag-document-ingest"
    lambda_batch_rows: int = 128
    lambda_artifact_bucket: str = "busrag-lambda-artifacts"
    lambda_opensearch_host: str = "host.containers.internal"
    lambda_openai_endpoint: str = ""
    lambda_embedding_endpoint: str = ""

    # --- Ingesta ---
    source_csv_path: str = "resources/data/BUI2.0 - BUI 2.0.csv"



@lru_cache
def get_settings() -> Settings:
    return Settings()
