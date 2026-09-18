import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Callable, Union, List

import backoff
import dspy
import requests
from bs4 import BeautifulSoup
from dsp import backoff_hdlr, giveup_hdlr

from .utils import WebPageHelper

logger = logging.getLogger(__name__)

_ARXIV_REQUEST_LOCK = threading.Lock()
_ARXIV_LAST_REQUEST_AT = 0.0
_ARXIV_MIN_INTERVAL_SECONDS = 3.1


class ArxivRM(dspy.Retrieve):
    """Retrieve paper metadata from the public arXiv API."""

    def __init__(
        self,
        k=3,
        endpoint="http://export.arxiv.org/api/query",
        sort_by="relevance",
        sort_order="descending",
        is_valid_source: Callable = None,
    ):
        super().__init__(k=k)
        self.k = k
        self.endpoint = endpoint
        self.sort_by = sort_by
        self.sort_order = sort_order
        self.usage = 0
        self.is_valid_source = is_valid_source or (lambda x: True)
        self._api_degraded = False

    def get_usage_and_reset(self):
        usage = self.usage
        self.usage = 0
        return {"ArxivRM": usage}

    def request(self, query: str):
        global _ARXIV_LAST_REQUEST_AT
        params = {
            "search_query": query,
            "start": 0,
            "max_results": self.k,
            "sortBy": self.sort_by,
            "sortOrder": self.sort_order,
        }
        headers = {
            "User-Agent": "PaperPilot-Agent/1.0 (academic research; contact via repository)"
        }
        with _ARXIV_REQUEST_LOCK:
            wait = _ARXIV_MIN_INTERVAL_SECONDS - (
                time.monotonic() - _ARXIV_LAST_REQUEST_AT
            )
            if wait > 0:
                time.sleep(wait)
            for attempt in range(2):
                try:
                    response = requests.get(
                        self.endpoint,
                        params=params,
                        headers=headers,
                        timeout=30,
                    )
                except (
                    requests.exceptions.ProxyError,
                    requests.exceptions.ConnectTimeout,
                ) as error:
                    proxy_values = " ".join(
                        os.getenv(name, "")
                        for name in (
                            "HTTP_PROXY",
                            "HTTPS_PROXY",
                            "http_proxy",
                            "https_proxy",
                        )
                    ).lower()
                    if not any(
                        host in proxy_values for host in ("127.0.0.1", "localhost")
                    ):
                        raise
                    logger.warning(
                        "Configured loopback proxy is unavailable; retrying arXiv directly: %s",
                        error,
                    )
                    session = requests.Session()
                    session.trust_env = False
                    response = session.get(
                        self.endpoint,
                        params=params,
                        headers=headers,
                        timeout=30,
                    )
                except requests.exceptions.RequestException:
                    self._api_degraded = True
                    raise
                finally:
                    _ARXIV_LAST_REQUEST_AT = time.monotonic()
                if response.status_code not in {429, 500, 502, 503, 504}:
                    response.raise_for_status()
                    return response.text
                if attempt < 1:
                    retry_after = response.headers.get("Retry-After", "")
                    delay = float(retry_after) if retry_after.isdigit() else 5.0 * (attempt + 1)
                    logger.warning(
                        "arXiv returned HTTP %s; retrying in %.1fs",
                        response.status_code,
                        delay,
                    )
                    time.sleep(delay)
            self._api_degraded = True
            response.raise_for_status()

    def _html_search(self, query: str):
        """Read-only fallback when export.arxiv.org is unavailable or throttled."""
        plain_query = re.sub(r"\b(?:all|ti|abs):", "", query, flags=re.I)
        plain_query = re.sub(r"\b(?:AND|OR|NOT)\b", " ", plain_query, flags=re.I)
        plain_query = re.sub(r"cat:[\w.-]+", " ", plain_query, flags=re.I)
        plain_query = re.sub(r"[()\"]", " ", plain_query)
        plain_query = re.sub(r"\s+", " ", plain_query).strip()
        if not plain_query:
            return []
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            "https://arxiv.org/search/",
            params={"query": plain_query, "searchtype": "all", "size": 25},
            headers={
                "User-Agent": "PaperPilot-Agent/1.0 (academic research; contact via repository)"
            },
            timeout=30,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        for item in soup.select("li.arxiv-result"):
            link = item.select_one('p.list-title a[href*="/abs/"]')
            title = item.select_one("p.title")
            abstract = item.select_one("span.abstract-full")
            if not link or not title or not abstract:
                continue
            url = str(link.get("href") or "").strip()
            description = " ".join(abstract.get_text(" ", strip=True).split())
            description = re.sub(r"\s*△ Less\s*$", "", description)
            authors = [node.get_text(" ", strip=True) for node in item.select("p.authors a")]
            result = {
                "url": url,
                "title": " ".join(title.get_text(" ", strip=True).split()),
                "description": description,
                "snippets": [description],
                "meta": {
                    "source_type": "arxiv_html_fallback",
                    "authors": authors,
                    "pdf_url": url.replace("/abs/", "/pdf/"),
                    "query": plain_query,
                },
            }
            if self._is_result_relevant_to_query(query, result):
                results.append(result)
            if len(results) >= self.k:
                break
        return results

    @staticmethod
    def _normalize_text(text):
        return " ".join((text or "").split())

    @staticmethod
    def _normalize_query_for_arxiv(query):
        query = " ".join((query or "").split())
        if not query:
            return ""

        normalized = query
        replacements = {
            "无源互调": "passive intermodulation",
            "小波": "wavelet",
            "神经网络": "neural network",
            "抑制": "suppression",
            "射频": "radio frequency",
        }
        for source, target in replacements.items():
            normalized = normalized.replace(source, f" {target} ")
        normalized = re.sub(r"\s+", " ", normalized).strip()

        lower_query = normalized.lower()
        has_pim = re.search(r"\bpim\b", lower_query) is not None
        passive_context_terms = (
            "passive intermodulation",
            "intermodulation",
            "suppression",
            "mitigation",
            "radio frequency",
            " rf",
            "antenna",
            "microwave",
            "neural network",
        )
        memory_context_terms = (
            "processing-in-memory",
            "processing in memory",
            "dram",
            " ram",
            "memory system",
        )

        if has_pim and any(term in lower_query for term in passive_context_terms):
            normalized = re.sub(
                r"\bpim\b", "passive intermodulation", normalized, flags=re.I
            )
        elif has_pim and any(term in lower_query for term in memory_context_terms):
            return ""

        return re.sub(r"\s+", " ", normalized).strip()

    @classmethod
    def _compile_queries_for_arxiv(cls, query):
        normalized = cls._normalize_query_for_arxiv(query)
        if not normalized:
            return []

        lowered = normalized.lower()
        compact = re.sub(r"\s+", "", lowered)
        is_muon_optimizer = (
            "muon优化器" in compact
            or "muon optimizer" in lowered
            or "orthogonalized momentum" in lowered
            or "momentum orthogonalized" in lowered
        )
        if is_muon_optimizer:
            return [
                'all:"Muon optimizer" AND (all:"neural network" OR all:"LLM training")',
                'all:"orthogonalized momentum" AND all:optimizer',
                'all:"Newton-Schulz" AND all:optimizer',
            ]
        if "wavelet" in lowered and "neural network" in lowered:
            return [
                'all:"wavelet neural network"',
                'all:wavelet AND all:"neural network"',
                'all:"deep wavelet neural network" AND (cat:cs.LG OR cat:cs.NE OR cat:eess.SP)',
            ]
        return [normalized]

    @staticmethod
    def _is_result_relevant_to_query(query, result):
        query = (query or "").lower()
        haystack = " ".join(
            [
                result.get("title") or "",
                result.get("description") or "",
                " ".join(result.get("snippets") or []),
            ]
        ).lower()
        is_muon_optimizer_query = any(
            marker in query
            for marker in (
                "muon optimizer",
                "orthogonalized momentum",
                "newton-schulz",
            )
        )
        if is_muon_optimizer_query:
            method_terms = (
                "muon",
                "orthogonalized momentum",
                "orthogonal momentum",
                "newton-schulz",
            )
            optimizer_terms = (
                "optimizer",
                "optimization",
                "neural network training",
                "llm training",
            )
            particle_terms = (
                "particle physics",
                "muon spectrometer",
                "muon detector",
                "muon anomalous magnetic moment",
                "muon collider",
                "muon decay",
            )
            return (
                any(term in haystack for term in method_terms)
                and any(term in haystack for term in optimizer_terms)
                and not any(term in haystack for term in particle_terms)
            )
        if "wavelet" in query and "neural network" in query:
            return (
                ("wavelet neural network" in haystack or "wavelet network" in haystack)
                and any(term in haystack for term in ("neural", "learning", "network"))
            )
        if "passive intermodulation" not in query:
            return True

        passive_terms = (
            "passive intermodulation",
            "intermodulation",
            "radio frequency",
            "rf ",
            "antenna",
            "microwave",
        )
        off_topic_terms = (
            "processing-in-memory",
            "processing in memory",
            "dram",
            " ram ",
            "memory system",
            "product information management",
        )
        return any(term in haystack for term in passive_terms) and not any(
            term in haystack for term in off_topic_terms
        )

    def _parse_response(self, response_text: str):
        namespace = {
            "atom": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom",
        }
        root = ET.fromstring(response_text)
        results = []

        for entry in root.findall("atom:entry", namespace):
            paper_id = self._normalize_text(entry.findtext("atom:id", namespaces=namespace))
            title = self._normalize_text(
                entry.findtext("atom:title", namespaces=namespace)
            )
            abstract = self._normalize_text(
                entry.findtext("atom:summary", namespaces=namespace)
            )
            published = self._normalize_text(
                entry.findtext("atom:published", namespaces=namespace)
            )
            updated = self._normalize_text(
                entry.findtext("atom:updated", namespaces=namespace)
            )
            authors = [
                self._normalize_text(author.findtext("atom:name", namespaces=namespace))
                for author in entry.findall("atom:author", namespace)
            ]
            authors = [author for author in authors if author]

            categories = [
                category.attrib.get("term")
                for category in entry.findall("atom:category", namespace)
                if category.attrib.get("term")
            ]
            primary_category = entry.find("arxiv:primary_category", namespace)
            primary_category_term = (
                primary_category.attrib.get("term")
                if primary_category is not None
                else None
            )

            abs_url = paper_id
            pdf_url = None
            for link in entry.findall("atom:link", namespace):
                href = link.attrib.get("href")
                if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                    pdf_url = href
                if link.attrib.get("rel") == "alternate" and href:
                    abs_url = href

            if not all([abs_url, title, abstract]):
                continue

            results.append(
                {
                    "url": abs_url,
                    "title": title,
                    "description": abstract,
                    "snippets": [abstract],
                    "meta": {
                        "source_type": "arxiv",
                        "authors": authors,
                        "published": published,
                        "updated": updated,
                        "categories": categories,
                        "primary_category": primary_category_term,
                        "pdf_url": pdf_url,
                    },
                }
            )

        return results

    def forward(
        self, query_or_queries: Union[str, List[str]], exclude_urls: List[str] = []
    ):
        queries = (
            [query_or_queries]
            if isinstance(query_or_queries, str)
            else query_or_queries
        )
        original_queries = [query for query in queries if query and query.strip()]
        compiled_queries = []
        for query in queries:
            if query and query.strip():
                compiled_queries.extend(self._compile_queries_for_arxiv(query))
        queries = list(dict.fromkeys(query for query in compiled_queries if query))
        self.usage += len(queries)

        collected_results = []
        seen_urls = set()
        for query in queries:
            try:
                results = self._parse_response(self.request(query))
            except Exception as e:
                logger.info("Skipping failed arXiv query %r: %s", query, e)
                if self._api_degraded:
                    break
                continue

            for result in results:
                url = result["url"]
                if (
                    url in seen_urls
                    or url in exclude_urls
                    or not self.is_valid_source(url)
                    or not self._is_result_relevant_to_query(query, result)
                ):
                    continue
                collected_results.append(result)
                seen_urls.add(url)
                if len(collected_results) >= self.k:
                    return collected_results

        if not collected_results and original_queries:
            try:
                fallback_query = self._compile_queries_for_arxiv(original_queries[0])[0]
                logger.warning("Using arXiv HTML fallback for query %r", fallback_query)
                for result in self._html_search(fallback_query):
                    if result["url"] not in seen_urls and result["url"] not in exclude_urls:
                        collected_results.append(result)
                        seen_urls.add(result["url"])
            except Exception as error:
                logger.info("arXiv HTML fallback failed: %s", error)
        return collected_results[: self.k]


class LocalPDFRM(dspy.Retrieve):
    """Retrieve relevant chunks from a local PDF collection."""

    def __init__(
        self,
        pdf_dir=None,
        documents=None,
        k=3,
        chunk_size=1200,
        chunk_overlap=150,
        is_valid_source: Callable = None,
    ):
        super().__init__(k=k)
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        self.k = k
        self.pdf_dir = pdf_dir
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.usage = 0
        self.is_valid_source = is_valid_source or (lambda x: True)
        self.chunks = []

        source_documents = list(documents or [])
        if pdf_dir:
            source_documents.extend(self._load_pdf_documents(pdf_dir))
        for document in source_documents:
            self._add_document(document)

    def get_usage_and_reset(self):
        usage = self.usage
        self.usage = 0
        return {"LocalPDFRM": usage}

    @staticmethod
    def _tokenize(text):
        return set(re.findall(r"[\w]+", (text or "").lower()))

    @staticmethod
    def _normalize_text(text):
        return " ".join((text or "").split())

    def _load_pdf_documents(self, pdf_dir):
        try:
            from pypdf import PdfReader
        except ImportError as err:
            raise ImportError("LocalPDFRM requires `pip install pypdf`.") from err

        documents = []
        for root, _, files in os.walk(pdf_dir):
            for filename in files:
                if not filename.lower().endswith(".pdf"):
                    continue
                path = os.path.join(root, filename)
                reader = PdfReader(path)
                pages = []
                for page in reader.pages:
                    pages.append(page.extract_text() or "")
                documents.append(
                    {
                        "title": os.path.splitext(filename)[0],
                        "path": path,
                        "text": "\n".join(pages),
                    }
                )
        return documents

    def _iter_chunks(self, text):
        text = self._normalize_text(text)
        if not text:
            return
        start = 0
        step = self.chunk_size - self.chunk_overlap
        while start < len(text):
            chunk = text[start : start + self.chunk_size].strip()
            if chunk:
                yield chunk
            start += step

    def _add_document(self, document):
        text = document.get("text", "")
        title = document.get("title") or os.path.basename(document.get("path", ""))
        path = document.get("path") or document.get("url") or title
        for index, chunk in enumerate(self._iter_chunks(text)):
            self.chunks.append(
                {
                    "url": f"{path}#chunk-{index}",
                    "title": title,
                    "description": f"Local PDF chunk from {title}",
                    "snippets": [chunk],
                    "meta": {
                        "source_type": "local_pdf",
                        "pdf_path": path,
                        "chunk_index": index,
                    },
                    "_tokens": self._tokenize(chunk),
                }
            )

    def forward(
        self, query_or_queries: Union[str, List[str]], exclude_urls: List[str] = []
    ):
        queries = (
            [query_or_queries]
            if isinstance(query_or_queries, str)
            else query_or_queries
        )
        queries = [query.strip() for query in queries if query and query.strip()]
        self.usage += len(queries)
        if not queries or not self.chunks:
            return []

        scored_results = {}
        for query in queries:
            query_tokens = self._tokenize(query)
            for chunk in self.chunks:
                url = chunk["url"]
                if url in exclude_urls or not self.is_valid_source(url):
                    continue
                score = len(query_tokens & chunk["_tokens"])
                if score <= 0:
                    continue
                if url not in scored_results or score > scored_results[url][0]:
                    scored_results[url] = (score, chunk)

        ranked_chunks = sorted(
            scored_results.values(), key=lambda item: item[0], reverse=True
        )
        results = []
        for _, chunk in ranked_chunks[: self.k]:
            result = {key: value for key, value in chunk.items() if key != "_tokens"}
            results.append(result)
        return results




class BingSearch(dspy.Retrieve):
    def __init__(
        self,
        bing_search_api_key=None,
        k=3,
        is_valid_source: Callable = None,
        min_char_count: int = 150,
        snippet_chunk_size: int = 1000,
        webpage_helper_max_threads=10,
        mkt="en-US",
        language="en",
        **kwargs,
    ):
        """
        Params:
            min_char_count: Minimum character count for the article to be considered valid.
            snippet_chunk_size: Maximum character count for each snippet.
            webpage_helper_max_threads: Maximum number of threads to use for webpage helper.
            mkt, language, **kwargs: Bing search API parameters.
            - Reference: https://learn.microsoft.com/en-us/bing/search-apis/bing-web-search/reference/query-parameters
        """
        super().__init__(k=k)
        if not bing_search_api_key and not os.environ.get("BING_SEARCH_API_KEY"):
            raise RuntimeError(
                "You must supply bing_search_subscription_key or set environment variable BING_SEARCH_API_KEY"
            )
        elif bing_search_api_key:
            self.bing_api_key = bing_search_api_key
        else:
            self.bing_api_key = os.environ["BING_SEARCH_API_KEY"]
        self.endpoint = "https://api.bing.microsoft.com/v7.0/search"
        self.params = {"mkt": mkt, "setLang": language, "count": k, **kwargs}
        self.webpage_helper = WebPageHelper(
            min_char_count=min_char_count,
            snippet_chunk_size=snippet_chunk_size,
            max_thread_num=webpage_helper_max_threads,
        )
        self.usage = 0

        # If not None, is_valid_source shall be a function that takes a URL and returns a boolean.
        if is_valid_source:
            self.is_valid_source = is_valid_source
        else:
            self.is_valid_source = lambda x: True

    def get_usage_and_reset(self):
        usage = self.usage
        self.usage = 0

        return {"BingSearch": usage}

    def forward(
        self, query_or_queries: Union[str, List[str]], exclude_urls: List[str] = []
    ):
        """Search with Bing for self.k top passages for query or queries

        Args:
            query_or_queries (Union[str, List[str]]): The query or queries to search for.
            exclude_urls (List[str]): A list of urls to exclude from the search results.

        Returns:
            a list of Dicts, each dict has keys of 'description', 'snippets' (list of strings), 'title', 'url'
        """
        queries = (
            [query_or_queries]
            if isinstance(query_or_queries, str)
            else query_or_queries
        )
        self.usage += len(queries)

        url_to_results = {}

        headers = {"Ocp-Apim-Subscription-Key": self.bing_api_key}

        for query in queries:
            try:
                results = requests.get(
                    self.endpoint, headers=headers, params={**self.params, "q": query}
                ).json()

                for d in results["webPages"]["value"]:
                    if self.is_valid_source(d["url"]) and d["url"] not in exclude_urls:
                        url_to_results[d["url"]] = {
                            "url": d["url"],
                            "title": d["name"],
                            "description": d["snippet"],
                        }
            except Exception as e:
                logging.error(f"Error occurs when searching query {query}: {e}")

        valid_url_to_snippets = self.webpage_helper.urls_to_snippets(
            list(url_to_results.keys())
        )
        collected_results = []
        for url in valid_url_to_snippets:
            r = url_to_results[url]
            r["snippets"] = valid_url_to_snippets[url]["snippets"]
            collected_results.append(r)

        return collected_results


class VectorRM(dspy.Retrieve):
    """Retrieve information from custom documents using Qdrant.

    To be compatible with STORM, the custom documents should have the following fields:
        - content: The main text content of the document.
        - title: The title of the document.
        - url: The URL of the document. STORM use url as the unique identifier of the document, so ensure different
            documents have different urls.
        - description (optional): The description of the document.
    The documents should be stored in a CSV file.
    """

    def __init__(
        self,
        collection_name: str,
        embedding_model: str,
        device: str = "mps",
        k: int = 3,
    ):
        from langchain_huggingface import HuggingFaceEmbeddings

        """
        Params:
            collection_name: Name of the Qdrant collection.
            embedding_model: Name of the Hugging Face embedding model.
            device: Device to run the embeddings model on, can be "mps", "cuda", "cpu".
            k: Number of top chunks to retrieve.
        """
        super().__init__(k=k)
        self.usage = 0
        # check if the collection is provided
        if not collection_name:
            raise ValueError("Please provide a collection name.")
        # check if the embedding model is provided
        if not embedding_model:
            raise ValueError("Please provide an embedding model.")

        model_kwargs = {"device": device}
        encode_kwargs = {"normalize_embeddings": True}
        self.model = HuggingFaceEmbeddings(
            model_name=embedding_model,
            model_kwargs=model_kwargs,
            encode_kwargs=encode_kwargs,
        )

        self.collection_name = collection_name
        self.client = None
        self.qdrant = None

    def _check_collection(self):
        from langchain_qdrant import Qdrant

        """
        Check if the Qdrant collection exists and create it if it does not.
        """
        if self.client is None:
            raise ValueError("Qdrant client is not initialized.")
        if self.client.collection_exists(collection_name=f"{self.collection_name}"):
            print(
                f"Collection {self.collection_name} exists. Loading the collection..."
            )
            self.qdrant = Qdrant(
                client=self.client,
                collection_name=self.collection_name,
                embeddings=self.model,
            )
        else:
            raise ValueError(
                f"Collection {self.collection_name} does not exist. Please create the collection first."
            )

    def init_online_vector_db(self, url: str, api_key: str):
        from qdrant_client import QdrantClient

        """
        Initialize the Qdrant client that is connected to an online vector store with the given URL and API key.

        Args:
            url (str): URL of the Qdrant server.
            api_key (str): API key for the Qdrant server.
        """
        if api_key is None:
            if not os.getenv("QDRANT_API_KEY"):
                raise ValueError("Please provide an api key.")
            api_key = os.getenv("QDRANT_API_KEY")
        if url is None:
            raise ValueError("Please provide a url for the Qdrant server.")

        try:
            self.client = QdrantClient(url=url, api_key=api_key)
            self._check_collection()
        except Exception as e:
            raise ValueError(f"Error occurs when connecting to the server: {e}")

    def init_offline_vector_db(self, vector_store_path: str):
        from qdrant_client import QdrantClient

        """
        Initialize the Qdrant client that is connected to an offline vector store with the given vector store folder path.

        Args:
            vector_store_path (str): Path to the vector store.
        """
        if vector_store_path is None:
            raise ValueError("Please provide a folder path.")

        try:
            self.client = QdrantClient(path=vector_store_path)
            self._check_collection()
        except Exception as e:
            raise ValueError(f"Error occurs when loading the vector store: {e}")

    def get_usage_and_reset(self):
        usage = self.usage
        self.usage = 0

        return {"VectorRM": usage}

    def get_vector_count(self):
        """
        Get the count of vectors in the collection.

        Returns:
            int: Number of vectors in the collection.
        """
        return self.qdrant.client.count(collection_name=self.collection_name)

    def forward(self, query_or_queries: Union[str, List[str]], exclude_urls: List[str]):
        """
        Search in your data for self.k top passages for query or queries.

        Args:
            query_or_queries (Union[str, List[str]]): The query or queries to search for.
            exclude_urls (List[str]): Dummy parameter to match the interface. Does not have any effect.

        Returns:
            a list of Dicts, each dict has keys of 'description', 'snippets' (list of strings), 'title', 'url'
        """
        queries = (
            [query_or_queries]
            if isinstance(query_or_queries, str)
            else query_or_queries
        )
        self.usage += len(queries)
        collected_results = []
        for query in queries:
            related_docs = self.qdrant.similarity_search_with_score(query, k=self.k)
            for i in range(len(related_docs)):
                doc = related_docs[i][0]
                collected_results.append(
                    {
                        "description": doc.metadata["description"],
                        "snippets": [doc.page_content],
                        "title": doc.metadata["title"],
                        "url": doc.metadata["url"],
                    }
                )

        return collected_results


class DuckDuckGoSearchRM(dspy.Retrieve):
    """Retrieve information from custom queries using DuckDuckGo."""

    def __init__(
        self,
        k: int = 3,
        is_valid_source: Callable = None,
        min_char_count: int = 150,
        snippet_chunk_size: int = 1000,
        webpage_helper_max_threads=10,
        safe_search: str = "On",
        region: str = "us-en",
    ):
        """
        Params:
            min_char_count: Minimum character count for the article to be considered valid.
            snippet_chunk_size: Maximum character count for each snippet.
            webpage_helper_max_threads: Maximum number of threads to use for webpage helper.
            **kwargs: Additional parameters for the OpenAI API.
        """
        super().__init__(k=k)
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
            except ImportError as err:
                raise ImportError(
                    "Duckduckgo requires `pip install ddgs` or `pip install duckduckgo_search`."
                ) from err
        self.k = k
        self.webpage_helper = WebPageHelper(
            min_char_count=min_char_count,
            snippet_chunk_size=snippet_chunk_size,
            max_thread_num=webpage_helper_max_threads,
        )
        self.usage = 0
        # All params for search can be found here:
        #   https://duckduckgo.com/duckduckgo-help-pages/settings/params/

        self.duck_duck_go_backend = "auto"

        # Only gets safe search results
        self.duck_duck_go_safe_search = safe_search

        # Specifies the region that the search will use
        self.duck_duck_go_region = region

        # If not None, is_valid_source shall be a function that takes a URL and returns a boolean.
        if is_valid_source:
            self.is_valid_source = is_valid_source
        else:
            self.is_valid_source = lambda x: True

        # Import the duckduckgo search library found here: https://github.com/deedy5/duckduckgo_search
        self.ddgs = DDGS()

    def get_usage_and_reset(self):
        usage = self.usage
        self.usage = 0
        return {"DuckDuckGoRM": usage}

    @backoff.on_exception(
        backoff.expo,
        (Exception,),
        max_time=1000,
        max_tries=8,
        on_backoff=backoff_hdlr,
        giveup=giveup_hdlr,
    )
    def request(self, query: str):
        results = self.ddgs.text(
            query,
            max_results=self.k,
            backend=self.duck_duck_go_backend,
        )
        return results

    def forward(
        self, query_or_queries: Union[str, List[str]], exclude_urls: List[str] = []
    ):
        """Search with DuckDuckGoSearch for self.k top passages for query or queries
        Args:
            query_or_queries (Union[str, List[str]]): The query or queries to search for.
            exclude_urls (List[str]): A list of urls to exclude from the search results.
        Returns:
            a list of Dicts, each dict has keys of 'description', 'snippets' (list of strings), 'title', 'url'
        """
        queries = (
            [query_or_queries]
            if isinstance(query_or_queries, str)
            else query_or_queries
        )
        self.usage += len(queries)

        collected_results = []

        for query in queries:
            #  list of dicts that will be parsed to return
            results = self.request(query)

            for d in results:
                # assert d is dict
                if not isinstance(d, dict):
                    print(f"Invalid result: {d}\n")
                    continue

                try:
                    # ensure keys are present
                    url = d.get("href", None)
                    title = d.get("title", None)
                    description = d.get("description", title)
                    snippets = [d.get("body", None)]

                    # raise exception of missing key(s)
                    if not all([url, title, description, snippets]):
                        raise ValueError(f"Missing key(s) in result: {d}")
                    if self.is_valid_source(url) and url not in exclude_urls:
                        result = {
                            "url": url,
                            "title": title,
                            "description": description,
                            "snippets": snippets,
                        }
                        collected_results.append(result)
                    else:
                        print(f"invalid source {url} or url in exclude_urls")
                except Exception as e:
                    print(f"Error occurs when processing {result=}: {e}\n")
                    print(f"Error occurs when searching query {query}: {e}")

        return collected_results
