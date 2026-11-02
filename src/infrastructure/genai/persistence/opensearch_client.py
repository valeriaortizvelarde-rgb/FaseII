"""Cliente compartido de OpenSearch."""
from opensearchpy import OpenSearch

from infrastructure.core.config import Settings, get_settings


def get_opensearch_client(settings: Settings | None = None) -> OpenSearch:
    settings = settings or get_settings()
    auth = (settings.opensearch_user, settings.opensearch_password) if settings.opensearch_password else None
    return OpenSearch(
        hosts=[{"host": settings.opensearch_host, "port": settings.opensearch_port}],
        http_auth=auth,
        use_ssl=settings.opensearch_use_ssl,
        verify_certs=settings.opensearch_verify_certs,
        ssl_show_warn=False,
    )
