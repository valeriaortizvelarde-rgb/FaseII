import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from infrastructure.core.config import Settings, get_settings


def get_s3_client(settings: Settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        config=Config(
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 2},
        ),
    )


def ensure_bucket(client, settings: Settings) -> None:
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket", "NotFound"):
            raise
        options = {"Bucket": settings.s3_bucket}
        if settings.s3_region != "us-east-1":
            options["CreateBucketConfiguration"] = {"LocationConstraint": settings.s3_region}
        try:
            client.create_bucket(**options)
        except ClientError as create_error:
            if create_error.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                raise


if __name__ == "__main__":
    settings = get_settings()
    ensure_bucket(get_s3_client(settings), settings)
    print(f"Bucket S3 disponible: {settings.s3_bucket}")