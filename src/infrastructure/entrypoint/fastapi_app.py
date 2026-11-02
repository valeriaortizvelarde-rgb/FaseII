"""Aplicación FastAPI: healthcheck y endpoint de chat RAG sobre el catálogo BUI."""
import logging
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import APIError, APITimeoutError
from opensearchpy import OpenSearch
from opensearchpy.exceptions import OpenSearchException
from pydantic import BaseModel, Field, field_validator
from pandas.errors import EmptyDataError, ParserError

from application.ingest import load_rows
from application.genai.llm.rag import answer_question
from infrastructure.core.config import Settings, get_settings
from infrastructure.genai.persistence.opensearch_client import get_opensearch_client
from infrastructure.genai.persistence.s3_client import get_s3_client
from infrastructure.genai.persistence.lambda_client import get_lambda_client, invoke_ingestion

logger = logging.getLogger("busrag")

STATIC_DIR = Path(__file__).resolve().parents[3] / "resources" / "static"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

app = FastAPI(title="Asistente del Catálogo BUI", version="0.1.0")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def get_client(settings: Settings = Depends(get_settings)) -> OpenSearch:
    return get_opensearch_client(settings)


def get_storage(settings: Settings = Depends(get_settings)):
    return get_s3_client(settings)


def get_processor(settings: Settings = Depends(get_settings)):
    return get_lambda_client(settings)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Pregunta del usuario sobre el catálogo BUI")

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("La pregunta no puede estar vacia.")
        return value


class Source(BaseModel):
    table_name: str
    row_id: int | None = None
    score: float | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source]


class DocumentUploadResponse(BaseModel):
    document_id: UUID
    filename: str
    row_count: int
    s3_bucket: str
    s3_key: str


class DocumentIngestRequest(BaseModel):
    document_id: UUID


class DocumentIngestResponse(BaseModel):
    document_id: UUID
    status: str
    chunk_count: int
    indexed_chunks: int


@app.post("/documents/upload", response_model=DocumentUploadResponse, status_code=201)
def upload_document(
    file: UploadFile,
    storage=Depends(get_storage),
    settings: Settings = Depends(get_settings),
):
    if not file.filename or Path(file.filename).suffix.lower() != ".csv":
        raise HTTPException(status_code=415, detail="Solo se admiten archivos CSV UTF-8.")
    document_id = uuid4()
    key = f"{document_id}.csv"
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="El archivo supera el limite de 20 MiB.")
    try:
        storage.put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=content,
            ContentType="text/csv",
            Metadata={"filename": quote(file.filename, safe="")},
        )
    except (BotoCoreError, ClientError) as exc:
        logger.exception("No se pudo guardar el original en S3")
        raise HTTPException(status_code=503, detail="No se pudo guardar el documento en S3. Revisa LocalStack y el bucket.") from exc
    try:
        rows = load_rows(BytesIO(content))
        if rows.empty:
            raise HTTPException(status_code=400, detail="El CSV no contiene filas de datos.")
    except (UnicodeError, EmptyDataError, ParserError, HTTPException) as exc:
        try:
            storage.delete_object(Bucket=settings.s3_bucket, Key=key)
        except (BotoCoreError, ClientError):
            logger.exception("No se pudo eliminar de S3 un CSV rechazado")
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=400, detail="El archivo no es un CSV UTF-8 valido.") from exc
    return DocumentUploadResponse(
        document_id=document_id, filename=file.filename, row_count=len(rows),
        s3_bucket=settings.s3_bucket, s3_key=key,
    )


@app.post("/documents/ingest", response_model=DocumentIngestResponse)
def ingest_document(
    request: DocumentIngestRequest,
    settings: Settings = Depends(get_settings),
    processor=Depends(get_processor),
):
    try:
        result = invoke_ingestion(processor, settings, str(request.document_id))
    except (BotoCoreError, ClientError) as exc:
        logger.exception("No se pudo invocar la Lambda de ingesta")
        raise HTTPException(status_code=503, detail="No se pudo invocar la Lambda. Ejecuta make setup-lambda y revisa LocalStack.") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="Respuesta invalida de la Lambda de ingesta.") from exc
    if result["status_code"] != 200:
        raise HTTPException(status_code=result["status_code"], detail=result.get("detail", "Error de ingesta en Lambda."))
    indexed_chunks = result["indexed_count"]
    chunk_count = indexed_chunks + result.get("error_count", 0)
    return DocumentIngestResponse(
        document_id=request.document_id,
        status="processed",
        chunk_count=chunk_count,
        indexed_chunks=indexed_chunks,
    )


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def healthcheck(
    client: OpenSearch = Depends(get_client),
    settings: Settings = Depends(get_settings),
    storage=Depends(get_storage),
    processor=Depends(get_processor),
):
    components = {"s3": "error", "lambda": "error", "opensearch": "error"}
    try:
        client.cluster.health()
        components["opensearch"] = "ok"
    except OpenSearchException:
        logger.warning("OpenSearch no esta disponible")

    try:
        storage.head_bucket(Bucket=settings.s3_bucket)
    except (BotoCoreError, ClientError):
        logger.warning("S3 o el bucket no estan disponibles")
    else:
        components["s3"] = "ok"

    try:
        processor.get_function_configuration(FunctionName=settings.lambda_function_name)
    except (BotoCoreError, ClientError):
        logger.warning("La funcion Lambda no esta disponible")
    else:
        components["lambda"] = "ok"

    return {"status": "ok" if all(value == "ok" for value in components.values()) else "error", **components}


@app.post("/ask", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    client: OpenSearch = Depends(get_client),
    settings: Settings = Depends(get_settings),
):
    try:
        if not client.indices.exists(index=settings.opensearch_index):
            raise HTTPException(
                status_code=503,
                detail=f"El índice '{settings.opensearch_index}' no existe. Ejecuta 'make ingest' primero.",
            )
    except OpenSearchException as exc:
        logger.exception("No se pudo conectar a OpenSearch")
        raise HTTPException(status_code=503, detail=f"No se pudo conectar a OpenSearch: {exc}") from exc

    try:
        result = answer_question(request.message, client, settings)
    except APITimeoutError as exc:
        logger.exception("Timeout esperando respuesta del modelo LLM")
        raise HTTPException(
            status_code=504,
            detail="El modelo tardó demasiado en responder. Intenta de nuevo.",
        ) from exc
    except APIError as exc:
        logger.exception("Error del proveedor LLM")
        raise HTTPException(status_code=502, detail=f"Error del proveedor de modelos: {exc}") from exc
    except OpenSearchException as exc:
        logger.exception("Error de OpenSearch durante la búsqueda")
        raise HTTPException(status_code=503, detail=f"Error al consultar OpenSearch: {exc}") from exc

    return ChatResponse(**result)

