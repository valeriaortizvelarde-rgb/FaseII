"""Ingesta del catálogo BUI (CSV) en OpenSearch: 1 chunk por fila + embeddings."""
from __future__ import annotations

from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

import pandas as pd
from opensearchpy import helpers
from tqdm import tqdm

from application.genai.llm.rag import embed_texts
from infrastructure.core.config import get_settings
from infrastructure.genai.persistence.opensearch_client import get_opensearch_client
from infrastructure.genai.persistence.s3_client import get_s3_client

MAX_FIELD_CHARS = 600
EMBEDDING_BATCH_SIZE = 16

TABLE_NAME_COLUMNS = [
    "Tabla ADA",
    "Nombre de la tabla en ADA Master LIVE",
    "Nombre de la tabla en ADA RAW",
    "Nombre Tabla Original",
    "Tabla o archivo a Ingestar",
]


def load_rows(csv_path: str | BinaryIO) -> pd.DataFrame:
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    return df


def resolve_table_name(row: pd.Series, fallback_name: str = "Registro") -> str:
    for column in TABLE_NAME_COLUMNS:
        value = str(row.get(column, "")).strip()
        if value:
            return value
    folio = str(row.get("Folio Data Hub", "")).strip()
    return f"Folio {folio}" if folio else fallback_name


def row_to_content(row: pd.Series) -> str:
    lines = []
    for column, value in row.items():
        value = str(value).strip()
        if not value:
            continue
        value = value.replace("\r", " ").replace("\n", " ").strip()
        if len(value) > MAX_FIELD_CHARS:
            value = value[:MAX_FIELD_CHARS] + "…"
        lines.append(f"{column.strip()}: {value}")
    return "\n".join(lines)


def ensure_index(client, index_name: str, embedding_dim: int) -> None:
    properties = {
        "document_id": {"type": "keyword"},
        "filename": {"type": "keyword"},
        "chunk_id": {"type": "keyword"},
        "chunk_position": {"type": "integer"},
        "chunk_text": {"type": "text"},
        "embedding": {
            "type": "knn_vector",
            "dimension": embedding_dim,
            "method": {
                "name": "hnsw",
                "space_type": "cosinesimil",
                "engine": "nmslib",
                "parameters": {"ef_construction": 128, "m": 24},
            },
        },
        "metadata": {
            "properties": {
                "source": {"type": "keyword"},
                "author": {"type": "keyword"},
                "department": {"type": "keyword"},
                "language": {"type": "keyword"},
                "table_name": {"type": "keyword"},
            }
        },
        "indexed_at": {"type": "date"},
    }
    if client.indices.exists(index=index_name):
        client.indices.put_mapping(
            index=index_name,
            body={"properties": {key: value for key, value in properties.items() if key != "embedding"}},
        )
        return
    body = {
        "settings": {
            "index": {
                "knn": True,
                "knn.algo_param.ef_search": 100,
            }
        },
        "mappings": {"properties": properties},
    }
    client.indices.create(index=index_name, body=body)
    print(f"Índice '{index_name}' creado con dimensión de embedding {embedding_dim}.")


def _column_value(row: pd.Series, *columns: str) -> str | None:
    for column in columns:
        value = str(row.get(column, "")).strip()
        if value:
            return value
    return None


def _row_metadata(row: pd.Series, table_name: str) -> dict:
    metadata = {"table_name": table_name}
    columns = {
        "source": ("Fuente Inicial", "Origen de la historia", "source"),
        "author": ("Responsable", "Data Owner (Data Availability)", "author"),
        "department": ("DOMINIO", "department"),
        "language": ("language", "idioma", "Idioma"),
    }
    for key, names in columns.items():
        value = _column_value(row, *names)
        if value:
            metadata[key] = value
    return metadata


def batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def ingest_csv(
    csv_path: str | BinaryIO, client, settings, document_id: str | None = None,
    start_row: int = 0, max_rows: int | None = None, filename: str | None = None,
) -> dict:
    document_id = document_id or "bui-catalog"
    filename = filename or f"{document_id}.csv"
    print(f"Leyendo CSV: {csv_path}")
    df = load_rows(csv_path)
    total_rows = len(df)
    if max_rows is not None:
        df = df.iloc[start_row : start_row + max_rows]
    print(f"{len(df)} filas encontradas. Generando 1 chunk por fila...")

    records = []
    for row_id, (_, row) in enumerate(df.iterrows(), start=start_row + 1):
        content = row_to_content(row)
        if not content:
            continue
        table_name = resolve_table_name(row, Path(filename).stem)
        records.append(
            {
                "chunk_position": row_id,
                "chunk_text": content,
                "metadata": _row_metadata(row, table_name),
            }
        )

    if not records:
        if max_rows is not None and total_rows:
            next_offset = start_row + len(df)
            return {
                "indexed_count": 0, "error_count": 0,
                "next_offset": next_offset if next_offset < total_rows else None,
            }
        raise ValueError("No hay registros para ingerir.")

    print(f"Generando embeddings para {len(records)} chunks (modelo: {settings.openai_embedding_model})...")
    indexed_count = 0
    errors = []
    index_initialized = False

    for batch in tqdm(list(batched(records, EMBEDDING_BATCH_SIZE))):
        texts = [item["chunk_text"] for item in batch]
        embeddings = embed_texts(texts, settings)
        if len(embeddings) != len(batch):
            raise ValueError("El numero de embeddings no coincide con el de registros.")
        for item, embedding in zip(batch, embeddings):
            item["embedding"] = embedding

        if not index_initialized:
            ensure_index(client, settings.opensearch_index, len(embeddings[0]))
            index_initialized = True

        actions = (
            {
                "_index": settings.opensearch_index,
                "_id": f"{document_id}:{item['chunk_position']}",
                "_source": {
                    "document_id": document_id,
                    "filename": filename,
                    "chunk_id": f"{document_id}_chunk_{item['chunk_position']:04d}",
                    "chunk_position": item["chunk_position"],
                    "chunk_text": item["chunk_text"],
                    "embedding": item["embedding"],
                    "metadata": item["metadata"],
                    "indexed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
            }
            for item in batch
        )
        batch_success, batch_errors = helpers.bulk(
            client,
            actions,
            raise_on_error=False,
            stats_only=False,
        )
        indexed_count += batch_success
        errors.extend(batch_errors)

    print(f"Ingesta completada: {indexed_count} documentos indexados, {len(errors)} errores.")
    if errors:
        print(errors[:5])
    client.indices.refresh(index=settings.opensearch_index)
    result = {"indexed_count": indexed_count, "error_count": len(errors)}
    if max_rows is not None:
        next_offset = start_row + len(df)
        result["next_offset"] = next_offset if next_offset < total_rows else None
    return result


def main() -> None:
    settings = get_settings()
    client = get_opensearch_client(settings)
    storage = get_s3_client(settings)
    path = Path(settings.source_csv_path)
    content = path.read_bytes()
    key = f"catalog/{path.name}"
    storage.put_object(
        Bucket=settings.s3_bucket, Key=key, Body=content,
        ContentType="text/csv", Metadata={"filename": quote(path.name, safe="")},
    )
    print(f"Original guardado en s3://{settings.s3_bucket}/{key}")
    ingest_csv(BytesIO(content), client, settings, document_id=path.stem, filename=path.name)


if __name__ == "__main__":
    main()
