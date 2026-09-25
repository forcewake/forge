"""Search corpus mappings (search-index)."""

PRODUCT_MAPPING = {
    "properties": {
        "sku": {"type": "keyword"},
        "title": {"type": "text"},
        "description": {"type": "text"},
        "price_cents": {"type": "integer"},
    }
}

#: How often the indexer pulls the catalog snapshot (seconds).
REFRESH_SECONDS = 300


def index_name(day: str) -> str:
    """The daily index name for a catalog refresh."""
    return f"products-{day}"
