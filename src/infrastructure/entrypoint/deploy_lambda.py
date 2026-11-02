import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import boto3
from botocore.exceptions import ClientError

from infrastructure.core.config import get_settings
from infrastructure.genai.persistence.lambda_client import get_lambda_client
from infrastructure.genai.persistence.s3_client import ensure_bucket, get_s3_client


def main():
    settings = get_settings()
    iam = boto3.client(
        "iam", endpoint_url=settings.s3_endpoint_url, region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
    )
    role_name = "busrag-lambda-role"
    try:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17", "Statement": [{
                    "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }],
            }),
        )["Role"]
    except iam.exceptions.EntityAlreadyExistsException:
        role = iam.get_role(RoleName=role_name)["Role"]
    iam.attach_role_policy(
        RoleName=role_name,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    iam.put_role_policy(
        RoleName=role_name, PolicyName="read-rag-documents",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{settings.s3_bucket}/*",
        }]}),
    )
    environment = {key.upper(): str(value) for key, value in settings.model_dump().items()}
    endpoint = urlsplit(settings.openai_endpoint)
    if endpoint.hostname in ("localhost", "127.0.0.1"):
        address = "host.containers.internal"
        if endpoint.port:
            address += f":{endpoint.port}"
        endpoint = endpoint._replace(netloc=address)
    embedding_endpoint = urlsplit(settings.embedding_endpoint)
    if embedding_endpoint.hostname in ("localhost", "127.0.0.1"):
        address = "host.containers.internal"
        if embedding_endpoint.port:
            address += f":{embedding_endpoint.port}"
        embedding_endpoint = embedding_endpoint._replace(netloc=address)
    environment.update({
        "S3_ENDPOINT_URL": "http://busrag-localstack:4566",
        "OPENSEARCH_HOST": settings.lambda_opensearch_host,
        "OPENAI_ENDPOINT": settings.lambda_openai_endpoint or urlunsplit(endpoint),
        "EMBEDDING_ENDPOINT": settings.lambda_embedding_endpoint or urlunsplit(embedding_endpoint),
    })
    client = get_lambda_client(settings)
    storage = get_s3_client(settings)
    artifact_settings = settings.model_copy(update={"s3_bucket": settings.lambda_artifact_bucket})
    ensure_bucket(storage, artifact_settings)
    artifact_key = "ingest-lambda.zip"
    storage.put_object(
        Bucket=settings.lambda_artifact_bucket, Key=artifact_key,
        Body=Path(".build/ingest-lambda.zip").read_bytes(), ContentType="application/zip",
    )
    code = {"S3Bucket": settings.lambda_artifact_bucket, "S3Key": artifact_key}
    config = {
        "FunctionName": settings.lambda_function_name,
        "Role": role["Arn"], "Timeout": 900, "MemorySize": 512,
        "Runtime": "python3.12", "Handler": "infrastructure.entrypoint.ingest_lambda.handler",
        "Environment": {"Variables": environment},
    }
    exists = True
    try:
        function = client.get_function(FunctionName=settings.lambda_function_name)
        if function["Configuration"].get("PackageType") != "Zip":
            client.delete_function(FunctionName=settings.lambda_function_name)
            exists = False
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        exists = False
    if not exists:
        architecture = os.environ.get("LAMBDA_ARCHITECTURE", "arm64")
        client.create_function(
            **config, PackageType="Zip", Code=code,
            Architectures=[architecture],
        )
        client.get_waiter("function_active_v2").wait(FunctionName=settings.lambda_function_name)
    else:
        client.update_function_code(
            FunctionName=settings.lambda_function_name,
            **code,
        )
        client.get_waiter("function_updated_v2").wait(FunctionName=settings.lambda_function_name)
        client.update_function_configuration(**config)
        client.get_waiter("function_updated_v2").wait(FunctionName=settings.lambda_function_name)
    print(f"Lambda disponible: {settings.lambda_function_name}")


if __name__ == "__main__":
    main()