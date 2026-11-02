import unittest
import json
from io import BytesIO
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from botocore.exceptions import ClientError, EndpointConnectionError
from opensearchpy.exceptions import OpenSearchException

from infrastructure.entrypoint import fastapi_app as api
from infrastructure.core.config import Settings
from application import ingest as ingestion
from application.genai.llm import rag
from infrastructure.entrypoint import ingest_lambda
from infrastructure.genai.persistence.lambda_client import invoke_ingestion


class DocumentEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.storage = MagicMock()
        self.storage.put_object.side_effect = self.put_object
        self.storage.get_object.side_effect = self.get_object
        self.storage.delete_object.side_effect = lambda **kwargs: self.objects.pop(kwargs["Key"], None)
        self.search = MagicMock()
        self.processor = MagicMock()
        self.processor.invoke.side_effect = self.invoke_lambda
        api.app.dependency_overrides[api.get_client] = lambda: self.search
        api.app.dependency_overrides[api.get_storage] = lambda: self.storage
        api.app.dependency_overrides[api.get_settings] = lambda: Settings(_env_file=None)
        api.app.dependency_overrides[api.get_processor] = lambda: self.processor
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)

    def put_object(self, **kwargs):
        self.assertEqual(kwargs["Bucket"], "rag-documents")
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        self.assertEqual(kwargs["Bucket"], "rag-documents")
        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": BytesIO(self.objects[kwargs["Key"]]), "Metadata": {"filename": "catalogo.csv"}}

    def invoke_lambda(self, **kwargs):
        with patch.object(ingest_lambda, "get_settings", return_value=Settings(_env_file=None)), patch.object(
            ingest_lambda, "get_s3_client", return_value=self.storage
        ), patch.object(ingest_lambda, "get_opensearch_client", return_value=self.search):
            result = ingest_lambda.handler(json.loads(kwargs["Payload"]), None)
        return {"Payload": BytesIO(json.dumps(result).encode())}

    def upload(self, content=b"Tabla ADA,Descripcion\nSALDOS,Saldos mensuales\n"):
        return self.client.post(
            "/documents/upload", files={"file": ("catalogo.csv", content, "text/csv")}
        )

    def test_upload_and_ingest(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        document = response.json()
        self.assertEqual(document["row_count"], 1)
        self.assertEqual(document["s3_bucket"], "rag-documents")
        self.assertEqual(self.objects[document["s3_key"]], b"Tabla ADA,Descripcion\nSALDOS,Saldos mensuales\n")
        actions_seen = []

        def bulk(client, actions, **kwargs):
            actions_seen.extend(actions)
            return 1, []

        with patch("application.ingest.embed_texts", return_value=[[0.1, 0.2]]), patch(
            "application.ingest.helpers.bulk", side_effect=bulk
        ):
            for attempt in range(2):
                result = self.client.post(
                    "/documents/ingest", json={"document_id": document["document_id"]}
                )
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json(), {
                    "document_id": document["document_id"],
                    "status": "processed",
                    "chunk_count": 1,
                    "indexed_chunks": 1,
                })
        self.assertEqual(actions_seen[0]["_id"], f"{document['document_id']}:1")
        self.assertEqual(actions_seen[0]["_id"], actions_seen[1]["_id"])
        indexed_source = actions_seen[0]["_source"]
        self.assertEqual(indexed_source["document_id"], document["document_id"])
        self.assertEqual(indexed_source["filename"], "catalogo.csv")
        self.assertEqual(indexed_source["chunk_id"], f"{document['document_id']}_chunk_0001")
        self.assertEqual(indexed_source["chunk_position"], 1)
        self.assertEqual(indexed_source["chunk_text"], "Tabla ADA: SALDOS\nDescripcion: Saldos mensuales")
        self.assertEqual(indexed_source["metadata"]["table_name"], "SALDOS")
        self.assertIn("indexed_at", indexed_source)
        self.search.indices.refresh.assert_called()
        self.assertEqual(self.processor.invoke.call_count, 2)
        self.assertEqual(self.processor.invoke.call_args.kwargs["FunctionName"], "busrag-document-ingest")

    def test_validation_file_required(self):
        response = self.client.post("/documents/upload")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"][0]["loc"], ["body", "file"])
        self.storage.put_object.assert_not_called()

    def test_validation_unsupported_file_type(self):
        for filename in ("document.pdf", "document.docx", "document.txt"):
            with self.subTest(filename=filename):
                response = self.client.post(
                    "/documents/upload",
                    files={"file": (filename, b"contenido", "application/octet-stream")},
                )
                self.assertEqual(response.status_code, 415)
                self.assertEqual(response.json()["detail"], "Solo se admiten archivos CSV UTF-8.")
        self.storage.put_object.assert_not_called()

    def test_validation_document_id_required_and_valid(self):
        for payload in ({}, {"document_id": None}, {"document_id": ""}, {"document_id": "invalid"}):
            with self.subTest(payload=payload):
                response = self.client.post("/documents/ingest", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"][0]["loc"], ["body", "document_id"])
        self.processor.invoke.assert_not_called()

    def test_validation_document_not_found(self):
        document_id = str(uuid4())
        detail = "Documento no encontrado en S3. Sube el CSV primero."
        with patch.object(api, "invoke_ingestion", return_value={"status_code": 404, "detail": detail}) as invoke:
            response = self.client.post("/documents/ingest", json={"document_id": document_id})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": detail})
        invoke.assert_called_once_with(self.processor, unittest.mock.ANY, document_id)

    def test_validation_question_required_and_not_blank(self):
        with patch.object(api, "answer_question") as answer:
            for payload in ({}, {"message": None}, {"message": ""}, {"message": "   "}, {"message": "\t\n"}):
                with self.subTest(payload=payload):
                    response = self.client.post("/ask", json=payload)
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json()["detail"][0]["loc"], ["body", "message"])
            answer.assert_not_called()
        self.search.indices.exists.assert_not_called()

    def test_validation_question_trims_surrounding_whitespace(self):
        with patch.object(api, "answer_question", return_value={"answer": "SALDOS", "sources": []}) as answer:
            response = self.client.post("/ask", json={"message": "  Tablas de saldos\n"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(answer.call_args.args[0], "Tablas de saldos")

    def test_reject_invalid_uploads(self):
        for content in (b"", b"Tabla ADA\n", b"Tabla ADA\n\xff\n", b'Tabla ADA\n"unterminated'):
            with self.subTest(content=content):
                self.assertEqual(self.upload(content).status_code, 400)
        result = self.client.post(
            "/documents/upload", files={"file": ("file.pdf", b"data", "application/pdf")}
        )
        self.assertEqual(result.status_code, 415)
        with patch.object(api, "MAX_UPLOAD_BYTES", 4):
            self.assertEqual(self.upload().status_code, 413)
        self.assertEqual(self.objects, {})

    def test_s3_persistence_before_processing(self):
        original_load_rows = api.load_rows

        def verify_saved(source):
            self.assertEqual(len(self.objects), 1)
            self.storage.put_object.assert_called_once()
            return original_load_rows(source)

        with patch.object(api, "load_rows", side_effect=verify_saved):
            self.assertEqual(self.upload().status_code, 201)

    def test_s3_failure_prevents_processing(self):
        self.storage.put_object.side_effect = EndpointConnectionError(endpoint_url="http://localhost:4566")
        with patch.object(api, "load_rows") as load_rows:
            self.assertEqual(self.upload().status_code, 503)
            load_rows.assert_not_called()
        self.storage.get_object.side_effect = EndpointConnectionError(endpoint_url="http://localhost:4566")
        with patch.object(ingest_lambda, "ingest_csv") as ingest:
            result = self.client.post("/documents/ingest", json={"document_id": str(uuid4())})
            self.assertEqual(result.status_code, 503)
            ingest.assert_not_called()

    def test_cli_saves_original_before_ingest(self):
        content = b"Tabla ADA\nSALDOS\n"

        def verify_saved(source, client, settings, **kwargs):
            self.storage.put_object.assert_called_once()
            self.assertEqual(source.read(), content)
            self.assertEqual(self.storage.put_object.call_args.kwargs["Body"], content)

        with patch.object(ingestion, "get_settings", return_value=Settings(_env_file=None)), patch.object(
            ingestion, "get_opensearch_client", return_value=self.search
        ), patch.object(ingestion, "get_s3_client", return_value=self.storage), patch.object(
            ingestion.Path, "read_bytes", return_value=content
        ), patch.object(ingestion, "ingest_csv", side_effect=verify_saved) as ingest:
            ingestion.main()
            ingest.assert_called_once()

    def test_lambda_reads_s3_and_indexes_batches(self):
        content = b"Tabla ADA,Descripcion\nSALDOS,Primera fila\nPAGOS,Segunda fila\n"
        document_id = str(uuid4())
        self.objects[f"{document_id}.csv"] = content
        actions_seen = []

        def bulk(client, actions, **kwargs):
            actions_seen.extend(actions)
            return 1, []

        settings = Settings(_env_file=None, lambda_batch_rows=1)
        with patch.object(ingest_lambda, "get_settings", return_value=settings), patch.object(
            ingest_lambda, "get_s3_client", return_value=self.storage
        ), patch.object(ingest_lambda, "get_opensearch_client", return_value=self.search), patch(
            "application.ingest.embed_texts", return_value=[[0.1, 0.2]]
        ), patch("application.ingest.helpers.bulk", side_effect=bulk):
            first = ingest_lambda.handler({"document_id": document_id}, None)
            self.assertEqual(first["status_code"], 200)
            self.assertEqual(first["next_offset"], 1)
            second = ingest_lambda.handler({"document_id": document_id, "offset": 1}, None)
            self.assertIsNone(second["next_offset"])
        self.assertEqual(
            [item["_id"] for item in actions_seen],
            [f"{document_id}:1", f"{document_id}:2"],
        )
        self.assertIn("Primera fila", actions_seen[0]["_source"]["chunk_text"])
        self.assertIn("Segunda fila", actions_seen[1]["_source"]["chunk_text"])

    def test_lambda_rejects_invalid_event(self):
        self.assertEqual(ingest_lambda.handler({}, None)["status_code"], 400)
        self.assertEqual(ingest_lambda.handler({"document_id": str(uuid4()), "offset": -1}, None)["status_code"], 400)

    def test_lambda_client_accumulates_batches(self):
        client = MagicMock()
        client.invoke.side_effect = [
            {"Payload": BytesIO(json.dumps({"status_code": 200, "indexed_count": 128, "error_count": 0, "next_offset": 128}).encode())},
            {"Payload": BytesIO(json.dumps({"status_code": 200, "indexed_count": 2, "error_count": 0, "next_offset": None}).encode())},
        ]
        result = invoke_ingestion(client, Settings(_env_file=None), str(uuid4()))
        self.assertEqual(result["indexed_count"], 130)
        self.assertEqual(json.loads(client.invoke.call_args.kwargs["Payload"])["offset"], 128)

    def test_lambda_runtime_error_and_invalid_continuation(self):
        self.processor.invoke.side_effect = None
        self.processor.invoke.return_value = {"FunctionError": "Unhandled", "Payload": BytesIO(b'{"errorMessage":"failure"}')}
        self.assertEqual(self.client.post("/documents/ingest", json={"document_id": str(uuid4())}).status_code, 502)
        self.processor.invoke.return_value = {"Payload": BytesIO(b'{"status_code":200,"indexed_count":1,"error_count":0,"next_offset":0}')}
        self.assertEqual(self.client.post("/documents/ingest", json={"document_id": str(uuid4())}).status_code, 502)

    def test_lambda_unavailable(self):
        self.processor.invoke.side_effect = EndpointConnectionError(endpoint_url="http://localhost:4566")
        self.assertEqual(self.client.post("/documents/ingest", json={"document_id": str(uuid4())}).status_code, 503)

    def test_health_reports_s3_unavailable(self):
        self.storage.head_bucket.side_effect = EndpointConnectionError(endpoint_url="http://localhost:4566")
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "status": "error", "s3": "error", "lambda": "ok", "opensearch": "ok",
        })

    def test_health_reports_lambda_unavailable(self):
        self.processor.get_function_configuration.side_effect = EndpointConnectionError(
            endpoint_url="http://localhost:4566"
        )
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "status": "error", "s3": "ok", "lambda": "error", "opensearch": "ok",
        })

    def test_unknown_and_invalid_document_id(self):
        self.assertEqual(self.client.post("/documents/ingest", json={"document_id": str(uuid4())}).status_code, 404)
        self.assertEqual(self.client.post("/documents/ingest", json={"document_id": "../../file"}).status_code, 422)

    def test_ingest_errors(self):
        document_id = self.upload().json()["document_id"]
        with patch.object(ingest_lambda, "ingest_csv", side_effect=OpenSearchException("unavailable")):
            self.assertEqual(self.client.post("/documents/ingest", json={"document_id": document_id}).status_code, 503)
        with patch.object(ingest_lambda, "ingest_csv", return_value={"indexed_count": 0, "error_count": 1}):
            self.assertEqual(self.client.post("/documents/ingest", json={"document_id": document_id}).status_code, 502)

    def test_required_routes_without_duplicates(self):
        self.search.cluster.health.return_value = {"status": "green"}
        self.assertEqual(self.client.get("/health").json(), {
            "status": "ok", "s3": "ok", "lambda": "ok", "opensearch": "ok",
        })
        with patch.object(api, "answer_question", return_value={"answer": "SALDOS", "sources": []}):
            for route in ("/ask",):
                result = self.client.post(route, json={"message": "Tablas de saldos"})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["answer"], "SALDOS")
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertEqual({"/health", "/ask", "/documents/upload", "/documents/ingest"}, set(paths))
        self.assertEqual(self.client.get("/healthcheck").status_code, 404)
        self.assertEqual(self.client.post("/chat", json={"message": "test"}).status_code, 404)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('fetch("/ask"', page.text)


class LocalEmbeddingModelTest(unittest.TestCase):
    def test_embeddings_use_local_all_minilm_even_with_azure_chat(self):
        settings = Settings(
            _env_file=None,
            llm_provider="azure",
            embedding_endpoint="http://localhost:11434/v1",
            embedding_api_key="ollama",
            openai_embedding_model="all-minilm",
        )
        client = MagicMock()
        client.embeddings.create.return_value.data = [MagicMock(embedding=[0.1] * 384)]

        with patch.object(rag, "_get_embedding_client", return_value=client) as get_client:
            vectors = rag.embed_texts(["texto de prueba"], settings)

        get_client.assert_called_once_with("http://localhost:11434/v1", "ollama")
        client.embeddings.create.assert_called_once_with(
            model="all-minilm", input=["texto de prueba"]
        )
        self.assertEqual(len(vectors[0]), 384)


if __name__ == "__main__":
    unittest.main()