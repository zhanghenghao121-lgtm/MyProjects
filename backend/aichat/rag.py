import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, TextLoader
from sentence_transformers import SentenceTransformer

RAG_CHROMA_DIR = os.environ.get("RAG_CHROMA_DIR", "media/aichat/chroma")
RAG_COLLECTION = os.environ.get("RAG_COLLECTION", "aichat_docs")
RAG_LOCAL_DOC_DIR = os.environ.get("RAG_LOCAL_DOC_DIR", "media/aichat/rag")
RAG_EMBED_MODEL_DIR = os.environ.get("RAG_EMBED_MODEL_DIR", "media/model/bge-m3")
RAG_CHUNK_SIZE = int(os.environ.get("RAG_CHUNK_SIZE", "1000"))
RAG_CHUNK_OVERLAP = int(os.environ.get("RAG_CHUNK_OVERLAP", "120"))
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "4"))
RAG_SCORE_THRESHOLD = float(os.environ.get("RAG_SCORE_THRESHOLD", "0.15"))
RAG_SUFFIXES = {".txt", ".md", ".pdf", ".docx"}

_LOCAL_DOC_SIGNATURE = ""
_EMBED_MODEL = None


@dataclass
class RetrievalItem:
    doc: Document
    score: float


class LocalBgeM3Embeddings(Embeddings):
    """Use local bge-m3 model for deterministic offline embeddings."""

    def __init__(self):
        self.model = _get_embed_model()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        vectors = self.model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return vectors.tolist()

    def embed_query(self, text: str) -> List[float]:
        vector = self.model.encode(
            [text],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return vector[0].tolist()


def _get_embed_model():
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        model_path = Path(RAG_EMBED_MODEL_DIR)
        if not model_path.exists():
            raise FileNotFoundError(f"本地 embedding 模型不存在: {model_path}")
        _EMBED_MODEL = SentenceTransformer(str(model_path))
    return _EMBED_MODEL


def _get_store() -> Chroma:
    return Chroma(
        collection_name=RAG_COLLECTION,
        embedding_function=LocalBgeM3Embeddings(),
        persist_directory=RAG_CHROMA_DIR,
    )


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _load_docs_from_path(path: str) -> List[Document]:
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".pdf":
        docs = PyPDFLoader(path).load()
    elif suffix == ".docx":
        docs = Docx2txtLoader(path).load()
    else:
        docs = TextLoader(path, encoding="utf-8", autodetect_encoding=True).load()
    for doc in docs:
        doc.page_content = _clean_text(doc.page_content)
    return [d for d in docs if d.page_content]


def _iter_local_doc_files() -> List[str]:
    root = os.path.abspath(RAG_LOCAL_DOC_DIR)
    if not os.path.isdir(root):
        return []
    files = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            suffix = os.path.splitext(filename)[1].lower()
            if suffix in RAG_SUFFIXES:
                files.append(os.path.join(dirpath, filename))
    files.sort()
    return files


def _build_local_signature(files: List[str]) -> str:
    hasher = hashlib.sha1()
    for path in files:
        try:
            stat = os.stat(path)
            hasher.update(path.encode("utf-8", "ignore"))
            hasher.update(str(stat.st_size).encode("utf-8"))
            hasher.update(str(int(stat.st_mtime)).encode("utf-8"))
        except OSError:
            continue
    return hasher.hexdigest()


def _stable_chunk_id(source_path: str, chunk_idx: int, content: str) -> str:
    base = f"{source_path}|{chunk_idx}|{hashlib.sha1(content.encode('utf-8', 'ignore')).hexdigest()}"
    return hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()


def _clear_store(store: Chroma):
    rows = store.get()
    ids = rows.get("ids", []) if isinstance(rows, dict) else []
    if ids:
        store.delete(ids=ids)


def _index_local_docs(files: List[str]) -> int:
    store = _get_store()
    _clear_store(store)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=RAG_CHUNK_SIZE,
        chunk_overlap=RAG_CHUNK_OVERLAP,
        separators=["\n\n", "\n", "。", "！", "？", " ", ""],
    )

    all_chunks: List[Document] = []
    all_ids: List[str] = []

    for path in files:
        suffix = os.path.splitext(path)[1].lower()
        if suffix not in RAG_SUFFIXES:
            continue
        docs = _load_docs_from_path(path)
        chunks = splitter.split_documents(docs)
        for idx, chunk in enumerate(chunks):
            content = _clean_text(chunk.page_content)
            if not content:
                continue
            metadata = {
                **(chunk.metadata or {}),
                "source_name": os.path.basename(path),
                "source_path": path,
                "chunk_index": idx,
            }
            all_chunks.append(Document(page_content=content, metadata=metadata))
            all_ids.append(_stable_chunk_id(path, idx, content))

    if all_chunks:
        store.add_documents(all_chunks, ids=all_ids)
    return len(all_chunks)


def sync_local_docs_if_needed(force: bool = False) -> dict:
    global _LOCAL_DOC_SIGNATURE
    files = _iter_local_doc_files()
    signature = _build_local_signature(files)
    chunks = -1
    if force or signature != _LOCAL_DOC_SIGNATURE:
        chunks = _index_local_docs(files)
        _LOCAL_DOC_SIGNATURE = signature
    return {"files": len(files), "chunks_reindexed": chunks}


def rebuild_local_docs() -> dict:
    sync_info = sync_local_docs_if_needed(force=True)
    store = _get_store()
    rows = store.get()
    ids = rows.get("ids", []) if isinstance(rows, dict) else []
    return {
        "files": int(sync_info.get("files", 0)),
        "chunks": len(ids),
        "collection": RAG_COLLECTION,
        "doc_dir": RAG_LOCAL_DOC_DIR,
        "embed_model_dir": RAG_EMBED_MODEL_DIR,
    }


def search_relevant(owner_id: int, query: str, k: int | None = None) -> List[Document]:
    _ = owner_id  # local knowledge base is global for current deployment
    q = (query or "").strip()
    if not q:
        return []
    sync_local_docs_if_needed()
    top_k = k if isinstance(k, int) and k > 0 else RAG_TOP_K
    store = _get_store()
    pairs: List[Tuple[Document, float]] = store.similarity_search_with_relevance_scores(q, k=top_k)
    docs: List[Document] = []
    for doc, score in pairs:
        if score is None or score >= RAG_SCORE_THRESHOLD:
            docs.append(doc)
    return docs


def owner_stats(owner_id: int) -> dict:
    _ = owner_id
    sync_local_docs_if_needed()
    store = _get_store()
    rows = store.get()
    ids = rows.get("ids", []) if isinstance(rows, dict) else []
    return {
        "chunks": len(ids),
        "collection": RAG_COLLECTION,
        "doc_dir": RAG_LOCAL_DOC_DIR,
        "embed_model_dir": RAG_EMBED_MODEL_DIR,
    }
