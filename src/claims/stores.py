"""Connections to the graph and vector stores, and the local embedding model."""

from functools import lru_cache

from neo4j import GraphDatabase
from qdrant_client import QdrantClient, models

from claims import config


@lru_cache(maxsize=1)
def neo4j():
    return GraphDatabase.driver(config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD))


def cypher(query: str, **params) -> list[dict]:
    records, _, _ = neo4j().execute_query(query, params, database_="neo4j")
    return [r.data() for r in records]


@lru_cache(maxsize=1)
def qdrant() -> QdrantClient:
    return QdrantClient(url=config.QDRANT_URL, timeout=30)


@lru_cache(maxsize=1)
def embedder():
    # Local ONNX model via fastembed: no API key, no network after the first download.
    from fastembed import TextEmbedding
    return TextEmbedding(config.EMBED_MODEL)


def embed(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in embedder().embed(texts)]


def recreate_collection(name: str, size: int = 384):
    client = qdrant()
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(name, vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE))
