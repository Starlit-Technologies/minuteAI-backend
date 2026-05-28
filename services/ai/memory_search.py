"""Institutional Memory Search.

After each meeting is processed, transcript chunks and minutes sections are
embedded into a ChromaDB vector store. Any user can then ask a natural-language
question and get a synthesised answer with source attribution — scoped strictly
to meetings they own or share via their workspace.

Storage: storage/memory_db/  (persistent ChromaDB)
Embeddings: OpenAI text-embedding-3-small if OPENAI_API_KEY is set,
            otherwise ChromaDB's built-in sentence-transformers (all-MiniLM-L6-v2).
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CHROMA_DIR = str(Path(__file__).parent.parent / "storage" / "memory_db")
COLLECTION  = "meeting_memory"

_collection = None


def _get_collection():
    global _collection
    if _collection is not None:
        return _collection
    try:
        import chromadb

        Path(CHROMA_DIR).mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=CHROMA_DIR)

        openai_key = os.getenv("OPENAI_API_KEY", "")
        if openai_key:
            from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
            ef = OpenAIEmbeddingFunction(
                api_key=openai_key,
                model_name="text-embedding-3-small",
            )
        else:
            ef = None  # chromadb default: sentence-transformers all-MiniLM-L6-v2

        _collection = client.get_or_create_collection(
            name=COLLECTION,
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("Memory search ready (path=%s, docs=%d)", CHROMA_DIR, _collection.count())
        return _collection
    except Exception as exc:
        logger.error("Memory search init failed: %s", exc)
        return None


# ── Chunking ───────────────────────────────────────────────────────────────────

def _word_chunks(text: str, size: int = 350, overlap: int = 50) -> list[str]:
    words = text.split()
    if len(words) <= size:
        return [text.strip()]
    chunks, step = [], size - overlap
    for i in range(0, len(words), step):
        c = " ".join(words[i: i + size])
        if c:
            chunks.append(c)
        if i + size >= len(words):
            break
    return chunks


def _speaker_chunks(transcript: str) -> list[str]:
    """Split at speaker-turn boundaries; merge short turns into ~350-word groups."""
    turns = re.split(r'\n(?=Speaker [^\:]+:)', transcript.strip())
    groups, buf, buf_words = [], [], 0
    for turn in turns:
        wc = len(turn.split())
        if buf_words + wc > 350 and buf:
            groups.append(" ".join(buf))
            buf, buf_words = [], 0
        buf.append(turn)
        buf_words += wc
    if buf:
        groups.append(" ".join(buf))
    return groups or _word_chunks(transcript)


# ── Public API ─────────────────────────────────────────────────────────────────

def embed_meeting(
    *,
    job_id: str,
    user_id: str,
    workspace_id: Optional[str],
    thread_id: Optional[str],
    transcript: Optional[str],
    minutes: Optional[dict],
    meeting_title: Optional[str],
    meeting_date: Optional[str],
):
    """Embed all content for a processed meeting into the vector store."""
    col = _get_collection()
    if not col:
        return

    base = {
        "job_id":        job_id,
        "user_id":       user_id,
        "workspace_id":  workspace_id or "",
        "thread_id":     thread_id    or "",
        "meeting_title": meeting_title or "Meeting",
        "meeting_date":  meeting_date  or "",
    }

    docs, metas, ids = [], [], []

    def _push(suffix: str, text: str, chunk_type: str):
        text = (text or "").strip()
        if not text:
            return
        docs.append(text)
        metas.append({**base, "chunk_type": chunk_type})
        ids.append(f"{job_id}_{suffix}")

    # transcript
    if transcript:
        chunks = _speaker_chunks(transcript)
        for i, c in enumerate(chunks):
            _push(f"tx{i}", c, "transcript")

    # minutes
    if minutes:
        _push("overview", minutes.get("overview") or "", "overview")

        for i, sec in enumerate((minutes.get("sections") or [])[:20]):
            body = "\n".join(
                p if isinstance(p, str) else (p.get("text") or p.get("point") or "")
                for p in (sec.get("points") or sec.get("content") or [])
            )
            _push(f"sec{i}", f"{sec.get('title', '')}\n{body}", "section")

        decisions = minutes.get("keyDecisions") or []
        if decisions:
            _push("decs", "Key decisions: " + " | ".join(
                d.get("decision", d) if isinstance(d, dict) else str(d)
                for d in decisions
            ), "decision")
            for i, d in enumerate(decisions):
                _push(f"dec{i}", d.get("decision", d) if isinstance(d, dict) else str(d), "decision")

        actions = minutes.get("actionItems") or []
        if actions:
            _push("acts", "Action items: " + " | ".join(
                (a.get("task", "") + (f" ({a['owner']})" if isinstance(a, dict) and a.get("owner") else ""))
                if isinstance(a, dict) else str(a)
                for a in actions
            ), "action_item")

        nxt = minutes.get("nextSteps") or []
        if nxt:
            _push("nxt", "Next steps: " + " | ".join(
                s.get("step", s) if isinstance(s, dict) else str(s)
                for s in nxt
            ), "next_steps")

    if not docs:
        return

    # remove stale chunks for this job before re-embedding
    try:
        old = col.get(where={"job_id": job_id})
        if old["ids"]:
            col.delete(ids=old["ids"])
    except Exception:
        pass

    try:
        col.add(documents=docs, metadatas=metas, ids=ids)
        logger.info("Embedded %d chunks for job %s ('%s')", len(docs), job_id, meeting_title)
    except Exception as exc:
        logger.error("Embed failed for job %s: %s", job_id, exc)


def delete_meeting(job_id: str):
    col = _get_collection()
    if not col:
        return
    try:
        old = col.get(where={"job_id": job_id})
        if old["ids"]:
            col.delete(ids=old["ids"])
    except Exception as exc:
        logger.warning("Delete embed failed for job %s: %s", job_id, exc)


def search_memory(
    *,
    query: str,
    user_id: str,
    workspace_id: Optional[str],
    thread_id: Optional[str] = None,
    n_results: int = 8,
) -> list[dict]:
    """
    Return relevant chunks for a natural-language query.
    Each result: {text, job_id, meeting_title, meeting_date, chunk_type, score}
    """
    col = _get_collection()
    if not col or not query.strip():
        return []

    total = col.count()
    if total == 0:
        return []

    where: dict = {}
    if thread_id:
        where["thread_id"] = thread_id
    elif workspace_id:
        where["workspace_id"] = workspace_id
    else:
        where["user_id"] = user_id

    try:
        res = col.query(
            query_texts=[query],
            n_results=min(n_results, total),
            where=where or None,
        )
    except Exception as exc:
        logger.error("Memory query failed: %s", exc)
        return []

    results = []
    if res and res.get("documents"):
        for doc, meta, dist in zip(
            res["documents"][0],
            res["metadatas"][0],
            (res.get("distances") or [[]])[0],
        ):
            results.append({
                "text":          doc,
                "job_id":        meta.get("job_id", ""),
                "meeting_title": meta.get("meeting_title", ""),
                "meeting_date":  meta.get("meeting_date", ""),
                "chunk_type":    meta.get("chunk_type", ""),
                "score":         round(1 - dist, 3),
            })
    return results
