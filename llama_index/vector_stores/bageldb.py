"""BagelDB vector store."""
import logging
import math
from typing import Any, List, cast

from llama_index.schema import MetadataMode, TextNode
from llama_index.utils import truncate_text
from llama_index.vector_stores.types import (
    MetadataFilters,
    NodeWithEmbedding,
    VectorStore,
    VectorStoreQuery,
    VectorStoreQueryResult,
)
from llama_index.vector_stores.utils import (
    legacy_metadata_dict_to_node,
    metadata_dict_to_node,
    node_to_metadata_dict,
)
import bagel

logger = logging.getLogger(__name__)


def _to_bageldb_filter(standard_filters: MetadataFilters) -> dict:
    """Translate standard metadata filters to BagelDB specific spec."""
    filters = {}
    for filter in standard_filters.filters:
        filters[filter.key] = filter.value
    return filters


class BagelDBVectorStore(VectorStore):
    """BagelDB vector store.

    In this vector store, embeddings are stored within a BagelDB cluster.

    During query time, the index uses BagelDB to query for the top
    k most similar nodes.

    Args:
        bageldb_cluster (bagel.api.models.Cluster.Cluster):
            BagelDB cluster instance

    """

    stores_text: bool = True
    flat_metadata: bool = True

    def __init__(self, bageldb_collection: Any, **kwargs: Any) -> None:
        """Init params."""
        import_err_msg = (
            "`bageldb_collection` package not found, please run `pip install betabageldb`"
        )
        try:
            import bagel  # noqa: F401
        except ImportError:
            raise ImportError(import_err_msg)
        from bagel.api.Cluster import Cluster

        self._cluster = cast(Cluster, bageldb_collection)

    def add(self, embedding_results: List[NodeWithEmbedding]) -> List[str]:
        """Add embedding results to index.

        Args
            embedding_results: List[NodeWithEmbedding]: list of embedding results

        """
        if not self._cluster:
            raise ValueError("Cluster not initialized")

        embeddings = []
        metadatas = []
        ids = []
        documents = []
        for result in embedding_results:
            embeddings.append(result.embedding)
            metadatas.append(
                node_to_metadata_dict(
                    result.node, remove_text=True, flat_metadata=self.flat_metadata
                )
            )
            ids.append(result.id)
            documents.append(
                result.node.get_content(metadata_mode=MetadataMode.NONE) or ""
            )

        self._cluster.add(
            embeddings=embeddings,
            ids=ids,
            metadatas=metadatas,
            documents=documents,
        )
        return ids

    def delete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        """
        Delete nodes using with ref_doc_id.

        Args:
            ref_doc_id (str): The doc_id of the document to delete.

        """
        self._cluster.delete(where={"document_id": ref_doc_id})

    @property
    def client(self) -> Any:
        """Return client."""
        return self._cluster

    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        """Query index for top k most similar nodes.

        Args:
            query_embedding (List[float]): query embedding
            similarity_top_k (int): top k most similar nodes

        """
        if query.filters is not None:
            if "where" in kwargs:
                raise ValueError(
                    "Cannot specify metadata filters via both query and kwargs. "
                    "Use kwargs only for BagelDB specific items that are "
                    "not supported via the generic query interface."
                )
            where = _to_bageldb_filter(query.filters)
        else:
            where = kwargs.pop("where", {})

        results = self._cluster.find(
            query_embeddings=query.query_embedding,
            n_results=query.similarity_top_k,
            where=where,
            **kwargs,
        )

        logger.debug(f"> Top {len(results['documents'])} nodes:")
        nodes = []
        similarities = []
        ids = []
        for node_id, text, metadata, distance in zip(
            results["ids"][0],
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            try:
                node = metadata_dict_to_node(metadata)
                node.set_content(text)
            except Exception:
                # NOTE: deprecated legacy logic for backward compatibility
                metadata, node_info, relationships = legacy_metadata_dict_to_node(
                    metadata
                )

                node = TextNode(
                    text=text,
                    id_=node_id,
                    metadata=metadata,
                    start_char_idx=node_info.get("start", None),
                    end_char_idx=node_info.get("end", None),
                    relationships=relationships,
                )

            nodes.append(node)

            similarity_score = 1.0 - math.exp(-distance)
            similarities.append(similarity_score)

            logger.debug(
                f"> [Node {node_id}] [Similarity score: {similarity_score}] "
                f"{truncate_text(str(text), 100)}"
            )
            ids.append(node_id)

        return VectorStoreQueryResult(nodes=nodes, similarities=similarities, ids=ids)
