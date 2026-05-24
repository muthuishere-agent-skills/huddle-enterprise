"""Storage smoke test: schema init, CRUD, search, parquet export. No claude calls."""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HUDDLE_ENTERPRISE_ROOT"] = tmp
        # Re-import after env is set so paths.root() picks it up.
        from wiki_builder import storage as st  # noqa: E402

        corpus = "test-corpus"
        with st.Store(corpus) as s:
            s.set_description("smoke test")
            s.upsert_doc(
                doc_id="doc1", source_path="/x/y", title="Doc One",
                doc_type="manual", num_pages=1, model="manual",
            )
            s.upsert_chunk(st.Chunk(
                chunk_id="doc1#0000", doc_id="doc1", corpus=corpus, ordinal=0,
                text="A K-line is a memory structure that connects mental agents.",
                summary="Definition of K-line",
                concepts=["k-line", "mental agent"],
                definitions={"k-line": "memory structure connecting agents"},
                claims=["K-lines connect agents that solved a problem together"],
                examples=[], xrefs=["society of mind"],
            ))
            s.upsert_chunk(st.Chunk(
                chunk_id="doc1#0001", doc_id="doc1", corpus=corpus, ordinal=1,
                text="Agents are simple, specialized processes. Many agents form a society.",
                summary="Definition of agents",
                concepts=["agent", "society of mind"],
                definitions={"agent": "simple specialized process"},
                claims=[], examples=[], xrefs=[],
            ))

            stats = s.stats()
            assert stats["docs"] == 1, stats
            assert stats["chunks"] == 2, stats
            assert stats["distinct_concepts"] >= 3, stats

            results = s.search("k-line")
            assert results, "search should find k-line"
            assert results[0]["chunk_id"] == "doc1#0000", results

            ok = s.patch_chunk("doc1#0001", {"summary": "Agents form societies"})
            assert ok
            got = s.get_chunk("doc1#0001")
            assert got["summary"] == "Agents form societies"

            qa_id = s.record_qa(
                question="What is a K-line?",
                answer="A memory structure...",
                personas=["maya", "suren"],
                chunk_refs=["doc1#0000"],
            )
            assert qa_id.startswith("qa-")
            recent = s.recent_qa()
            assert recent and recent[0]["question"] == "What is a K-line?"

            targets = s.export_parquet()
            for name, path in targets.items():
                assert Path(path).exists(), f"{name} parquet missing"

            ok = s.delete_chunk("doc1#0001")
            assert ok
            ok = s.delete_chunk("doc1#nonexistent")
            assert not ok

            n = s.delete_doc("doc1")
            assert n == 1, n
            assert s.stats()["chunks"] == 0

        corpora = st.list_corpora()
        assert any(c["corpus"] == corpus for c in corpora), corpora

        ok = st.drop_corpus(corpus)
        assert ok
        assert not (Path(tmp) / corpus).exists()

    print("OK storage smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
