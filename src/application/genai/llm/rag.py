"""Lógica RAG: generación de embeddings, búsqueda vectorial en OpenSearch y generación de respuesta con guardrails."""
from __future__ import annotations

from functools import lru_cache
import re

from openai import AzureOpenAI, OpenAI
from opensearchpy import OpenSearch

from infrastructure.core.config import Settings, get_settings

TOP_K = 10
TOP_CONTEXT = 3
LEXICAL_STOPWORDS = {
    "para", "una", "unos", "unas", "que", "con", "del", "los", "las", "por", "sobre",
    "tabla", "tablas", "informacion", "información", "necesito", "busco", "quiero", "contenga",
}

SYSTEM_PROMPT = """Eres un asistente que responde preguntas usando únicamente los registros recuperados del archivo \
que el usuario cargó. El archivo puede contener viajes u otro tipo de datos tabulares. Tu objetivo es encontrar \
los registros que mejor correspondan a la pregunta y explicarlos con claridad.

REGLAS ESTRICTAS (guardrails):
1. Fuente de verdad: responde exclusivamente usando los registros recuperados. No inventes datos ni completes con \
    conocimiento externo.
2. Si la pregunta pide un total, promedio u otra operación sobre todo el archivo que no pueda calcularse con los \
    registros recuperados, explica que esta búsqueda parcial no permite determinarlo.
3. Menciona fechas, distancias, lugares y propósitos únicamente cuando estén presentes en los registros.
4. Si los registros no bastan para responder, dilo claramente y pide una pregunta más específica.
"""

USER_PROMPT_TEMPLATE = """PREGUNTA DEL USUARIO:
{question}

REGISTROS RECUPERADOS (ordenados por relevancia):
{context}

ETIQUETAS DE LOS REGISTROS:
{candidate_tables}

Usa solo estos registros para responder. Si no contienen evidencia suficiente, indícalo.
"""


@lru_cache
def get_openai_client() -> AzureOpenAI | OpenAI:
    settings = get_settings()
    if settings.llm_provider == "ollama":
        # Ollama expone una API compatible con OpenAI en /v1; no requiere api_key real.
        return OpenAI(base_url=settings.openai_endpoint, api_key=settings.openai_api_key, timeout=90.0)
    return AzureOpenAI(
        azure_endpoint=settings.openai_endpoint,
        api_key=settings.openai_api_key,
        api_version=settings.openai_api_version,
        timeout=90.0,
    )


@lru_cache
def _get_embedding_client(endpoint: str, api_key: str) -> OpenAI:
    return OpenAI(base_url=endpoint, api_key=api_key, timeout=90.0)


def get_embedding_client(settings: Settings | None = None) -> OpenAI:
    settings = settings or get_settings()
    return _get_embedding_client(settings.embedding_endpoint, settings.embedding_api_key)


def embed_text(text: str, settings: Settings | None = None) -> list[float]:
    settings = settings or get_settings()
    client = get_embedding_client(settings)
    response = client.embeddings.create(model=settings.openai_embedding_model, input=text)
    return response.data[0].embedding


def embed_texts(texts: list[str], settings: Settings | None = None) -> list[list[float]]:
    settings = settings or get_settings()
    client = get_embedding_client(settings)
    response = client.embeddings.create(model=settings.openai_embedding_model, input=texts)
    return [item.embedding for item in response.data]


def _chunk_from_hit(hit: dict) -> dict:
    source = hit["_source"]
    metadata = source.get("metadata") or {}
    return {
        "score": hit.get("_score"),
        "content": source.get("chunk_text", source.get("content", "")),
        "row_id": source.get("chunk_position", source.get("row_id")),
        "table_name": metadata.get("table_name") or source.get("table_name", ""),
    }


def search_top_chunks(
    client: OpenSearch,
    query_embedding: list[float],
    question: str,
    index_name: str,
    k: int = TOP_K,
) -> list[dict]:
    query = {
        "size": k,
        "query": {
            "knn": {
                "embedding": {
                    "vector": query_embedding,
                    "k": k,
                }
            }
        },
    }
    response = client.search(index=index_name, body=query)
    hits = response.get("hits", {}).get("hits", [])
    semantic_chunks = [_chunk_from_hit(hit) for hit in hits]

    lexical_terms = [
        term for term in re.findall(r"(?iu)[a-záéíóúñ0-9]+", question)
        if len(term) >= 3 and term.lower() not in LEXICAL_STOPWORDS
    ]
    lexical_chunks = []
    if lexical_terms:
        lexical_response = client.search(
            index=index_name,
            body={
                "size": k,
                "query": {
                    "bool": {
                        "should": [
                            {"match": {"chunk_text": {"query": " ".join(lexical_terms), "minimum_should_match": "60%"}}},
                            {"match": {"content": {"query": " ".join(lexical_terms), "minimum_should_match": "60%"}}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
            },
        )
        lexical_chunks = [
            _chunk_from_hit(hit) for hit in lexical_response.get("hits", {}).get("hits", [])
        ]

    exact_names = set(re.findall(r"(?i)\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", question))
    if not exact_names:
        candidates = lexical_chunks + semantic_chunks
        seen_tables = set()
        return [
            chunk for chunk in candidates
            if not (chunk["table_name"] in seen_tables or seen_tables.add(chunk["table_name"]))
        ][:k]

    exact_response = client.search(
        index=index_name,
        body={
            "size": len(exact_names),
            "query": {
                "bool": {
                    "should": [
                        {"terms": {"table_name": list(exact_names)}},
                        {"terms": {"metadata.table_name": list(exact_names)}},
                        {"match_phrase": {"content": question}},
                        {"match_phrase": {"chunk_text": question}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        },
    )
    exact_chunks = [_chunk_from_hit(hit) for hit in exact_response.get("hits", {}).get("hits", [])]

    seen_tables = {chunk["table_name"] for chunk in exact_chunks}
    remaining = []
    for chunk in lexical_chunks + semantic_chunks:
        if chunk["table_name"] in seen_tables:
            continue
        seen_tables.add(chunk["table_name"])
        remaining.append(chunk)
    return (exact_chunks + remaining)[:k]


def build_candidate_tables(chunks: list[dict]) -> str:
    names = []
    for chunk in chunks:
        name = (chunk.get("table_name") or "").strip()
        if name and name not in names:
            names.append(name)
    if not names:
        return "(sin coincidencias claras)"
    return "\n".join(f"- {name}" for name in names)


def answer_question(
    question: str,
    opensearch_client: OpenSearch,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()

    query_embedding = embed_text(question, settings)
    top_chunks = search_top_chunks(
        opensearch_client, query_embedding, question, settings.opensearch_index, k=TOP_K
    )

    explicit_table_names = set(re.findall(r"(?i)\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", question))
    if explicit_table_names:
        exact_context = [
            chunk for chunk in top_chunks if chunk["table_name"].lower() in {name.lower() for name in explicit_table_names}
        ]
        context_chunks = exact_context[:TOP_CONTEXT] or top_chunks[:TOP_CONTEXT]
        candidate_tables = "\n".join(f"- {chunk['table_name']}" for chunk in context_chunks)
    else:
        context_chunks = top_chunks[:TOP_CONTEXT]
        candidate_tables = build_candidate_tables(top_chunks)
    context = "\n\n---\n\n".join(chunk["content"] for chunk in context_chunks) or "(sin contexto disponible)"

    user_prompt = USER_PROMPT_TEMPLATE.format(
        top_context=TOP_CONTEXT,
        context=context,
        top_k=TOP_K,
        candidate_tables=candidate_tables,
        question=question,
    )

    client = get_openai_client()
    completion = client.chat.completions.create(
        model=settings.openai_chat_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    answer = completion.choices[0].message.content

    source_chunks = context_chunks if explicit_table_names else top_chunks
    return {
        "answer": answer,
        "sources": [
            {"table_name": chunk["table_name"], "row_id": chunk["row_id"], "score": chunk["score"]}
            for chunk in source_chunks
        ],
    }
