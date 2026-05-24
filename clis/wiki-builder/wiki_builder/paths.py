"""Filesystem layout for huddle-enterprise wiki storage."""
from __future__ import annotations
import os
from pathlib import Path

ROOT_ENV = "HUDDLE_ENTERPRISE_ROOT"


def root() -> Path:
    override = os.environ.get(ROOT_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "muthuishere-agent-skills" / "huddle-enterprise"


def corpus_dir(corpus: str) -> Path:
    return root() / corpus


def db_path(corpus: str) -> Path:
    return corpus_dir(corpus) / "wiki.duckdb"


def exports_dir(corpus: str) -> Path:
    return corpus_dir(corpus) / "exports"


def qa_dir(corpus: str) -> Path:
    return corpus_dir(corpus) / "qa"


def ensure_corpus_dirs(corpus: str) -> Path:
    cdir = corpus_dir(corpus)
    (cdir / "exports").mkdir(parents=True, exist_ok=True)
    (cdir / "qa").mkdir(parents=True, exist_ok=True)
    return cdir
