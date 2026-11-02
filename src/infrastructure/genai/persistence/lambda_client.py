import json

import boto3
from botocore.config import Config

from infrastructure.core.config import Settings


def get_lambda_client(settings: Settings):
    return boto3.client(
        "lambda",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        config=Config(connect_timeout=5, read_timeout=910, retries={"max_attempts": 0}),
    )


def invoke_ingestion(client, settings: Settings, document_id: str) -> dict:
    result = {"document_id": document_id, "indexed_count": 0, "error_count": 0}
    offset = 0
    while True:
        response = client.invoke(
            FunctionName=settings.lambda_function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps({"document_id": document_id, "offset": offset}).encode(),
        )
        body = response["Payload"]
        try:
            payload = json.loads(body.read())
        finally:
            body.close()
        if response.get("FunctionError"):
            return {"status_code": 502, "detail": "La Lambda fallo. Revisa sus logs en LocalStack."}
        if payload.get("status_code") != 200:
            return payload
        result["indexed_count"] += payload["indexed_count"]
        result["error_count"] += payload["error_count"]
        if result["error_count"]:
            return {"status_code": 502, "detail": {"message": "Ingesta parcial; reintenta con el mismo document_id.", **result}}
        next_offset = payload.get("next_offset")
        if next_offset is None:
            return {"status_code": 200, **result}
        if type(next_offset) is not int or next_offset <= offset:
            return {"status_code": 502, "detail": "La Lambda devolvio una continuacion invalida."}
        offset = next_offset