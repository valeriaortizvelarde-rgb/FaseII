import logging
from io import BytesIO
from urllib.parse import unquote
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from openai import APIError, APITimeoutError
from opensearchpy.exceptions import OpenSearchException
from pandas.errors import ParserError

from application.ingest import ingest_csv
from infrastructure.core.config import get_settings
from infrastructure.genai.persistence.opensearch_client import get_opensearch_client
from infrastructure.genai.persistence.s3_client import get_s3_client

logger = logging.getLogger(__name__)
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024


def handler(event, context):
    try:
        document_id = str(UUID(event["document_id"]))
        offset = event.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset debe ser un entero no negativo.")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        return {"status_code": 400, "detail": str(exc)}

    settings = get_settings()
    try:
        storage = get_s3_client(settings)
        response = storage.get_object(Bucket=settings.s3_bucket, Key=f"{document_id}.csv")
        body = response["Body"]
        try:
            content = body.read(MAX_DOCUMENT_BYTES + 1)
        finally:
            body.close()
        if len(content) > MAX_DOCUMENT_BYTES:
            return {"status_code": 413, "detail": "El archivo supera el limite de 20 MiB."}
        result = ingest_csv(
            BytesIO(content), get_opensearch_client(settings), settings, document_id,
            start_row=offset, max_rows=max(1, min(settings.lambda_batch_rows, 256)),
            filename=unquote(response.get("Metadata", {}).get("filename", f"{document_id}.csv")),
        )
        return {"status_code": 200, "document_id": document_id, **result}
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            return {"status_code": 404, "detail": "Documento no encontrado en S3. Sube el CSV primero."}
        logger.exception("Error S3 en Lambda")
        return {"status_code": 503, "detail": "No se pudo recuperar el documento de S3."}
    except BotoCoreError:
        return {"status_code": 503, "detail": "No se pudo conectar con S3 en LocalStack."}
    except APITimeoutError:
        return {"status_code": 504, "detail": "Tiempo de espera agotado generando embeddings."}
    except APIError:
        return {"status_code": 502, "detail": "Error del proveedor de embeddings."}
    except OpenSearchException:
        return {"status_code": 503, "detail": "Error de OpenSearch durante la ingesta."}
    except (ValueError, UnicodeError, ParserError) as exc:
        return {"status_code": 400, "detail": str(exc)}