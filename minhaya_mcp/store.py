"""SQLite notebook. All content stays on disk; there are no network calls."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


FIELDS = ("question", "answer", "reading", "explanation", "category", "tags",
          "source_url", "source_note", "status")
STATUSES = {"draft", "ready", "entered"}
ALIASES = {"問題": "question", "問題文": "question", "答え": "answer", "解答": "answer",
           "正解": "answer", "読み": "reading", "解説": "explanation", "ジャンル": "category",
           "タグ": "tags", "出典URL": "source_url", "出典メモ": "source_note", "状態": "status"}


def normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate(values: dict) -> dict:
    result = {key: values.get(key, "") for key in FIELDS}
    result["status"] = result["status"] or "draft"
    for key in FIELDS:
        if key == "tags":
            continue
        if not isinstance(result[key], str):
            raise ValueError(f"{key} は文字列で指定してください。")
        result[key] = result[key].strip()
        if "\x00" in result[key] or len(result[key]) > 20000:
            raise ValueError(f"{key} が長すぎるか、無効な文字を含みます。")
    if not result["question"] or not result["answer"]:
        raise ValueError("問題文と答えは必須です。")
    if result["status"] not in STATUSES:
        raise ValueError("状態は draft / ready / entered で指定してください。")
    url = result["source_url"]
    if url and (urlparse(url).scheme not in {"https", "http"} or not urlparse(url).netloc):
        raise ValueError("出典URLは http または https のURLで指定してください。")
    tags = result["tags"] or []
    if isinstance(tags, str):
        tags = re.split(r"[|、]", tags)
    if not isinstance(tags, list) or len(tags) > 50 or any(not isinstance(t, str) for t in tags):
        raise ValueError("タグは50個以内の文字列リストで指定してください。")
    result["tags"] = list(dict.fromkeys(t.strip() for t in tags if t.strip()))
    if any(len(t) > 100 or "\x00" in t or "|" in t or "、" in t for t in result["tags"]):
        raise ValueError("タグは100文字以内とし、区切り文字 | 、を含めないでください。")
    return result


class Notebook:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS quizzes (
                    id TEXT PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL,
                    reading TEXT NOT NULL, explanation TEXT NOT NULL, category TEXT NOT NULL,
                    tags TEXT NOT NULL, source_url TEXT NOT NULL, source_note TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft','ready','entered')),
                    question_key TEXT NOT NULL UNIQUE, search_text TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY, quiz_id TEXT NOT NULL REFERENCES quizzes(id),
                    correct INTEGER NOT NULL CHECK(correct IN (0,1)), reviewed_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row) -> dict:
        item = dict(row)
        item["tags"] = json.loads(item["tags"])
        item.pop("question_key", None)
        item.pop("search_text", None)
        return item

    @staticmethod
    def key(question: str) -> str:
        return hashlib.sha256(normalize(question).encode("utf-8")).hexdigest()

    @staticmethod
    def params(item: dict) -> dict:
        return dict(item, tags=json.dumps(item["tags"], ensure_ascii=False),
                    question_key=Notebook.key(item["question"]),
                    search_text=normalize(" ".join(str(item[k]) for k in FIELDS)))

    def get(self, quiz_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        if row is None:
            raise ValueError("指定された問題IDがありません。")
        return self.decode(row)

    def _insert(self, db, item: dict) -> dict:
        existing = db.execute("SELECT * FROM quizzes WHERE question_key=?",
                              (self.key(item["question"]),)).fetchone()
        if existing:
            if normalize(existing["answer"]) != normalize(item["answer"]):
                raise ValueError(f"同じ問題文に別の答えが登録されています: {existing['id']}")
            return {"created": False, "quiz": self.decode(existing)}
        stamp = now()
        params = dict(self.params(item), id=uuid.uuid4().hex, created_at=stamp, updated_at=stamp)
        db.execute("""INSERT INTO quizzes VALUES (
            :id,:question,:answer,:reading,:explanation,:category,:tags,:source_url,
            :source_note,:status,:question_key,:search_text,:created_at,:updated_at
        )""", params)
        return {"created": True, "quiz": {"id": params["id"], **item,
                                          "created_at": stamp, "updated_at": stamp}}

    def create(self, **values) -> dict:
        item = validate(values)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._insert(db, item)

    def update(self, quiz_id: str, **changes) -> dict:
        changes = {key: value for key, value in changes.items() if value is not None}
        if set(changes) - set(FIELDS):
            raise ValueError("更新できない項目が含まれています。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
            if row is None:
                raise ValueError("指定された問題IDがありません。")
            item = validate({**self.decode(row), **changes})
            params = dict(self.params(item), id=quiz_id, updated_at=now())
            try:
                db.execute("""UPDATE quizzes SET question=:question,answer=:answer,
                    reading=:reading,explanation=:explanation,category=:category,tags=:tags,
                    source_url=:source_url,source_note=:source_note,status=:status,
                    question_key=:question_key,search_text=:search_text,updated_at=:updated_at
                    WHERE id=:id""", params)
            except sqlite3.IntegrityError as error:
                raise ValueError("同じ問題文が別IDで登録されています。") from error
        return self.get(quiz_id)

    def search(self, query="", category="", tag="", status="", limit=30, offset=0) -> dict:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("limit は1〜100、offset は0以上で指定してください。")
        if status and status not in STATUSES:
            raise ValueError("状態は draft / ready / entered で指定してください。")
        conditions, params = [], []
        for token in query.split():
            conditions.append("instr(search_text,?)>0")
            params.append(normalize(token))
        if category:
            conditions.append("category=?")
            params.append(category)
        if tag:
            conditions.append("EXISTS(SELECT 1 FROM json_each(quizzes.tags) WHERE value=?)")
            params.append(tag)
        if status:
            conditions.append("status=?")
            params.append(status)
        clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM quizzes" + clause, params).fetchone()[0]
            rows = db.execute("SELECT * FROM quizzes" + clause +
                              " ORDER BY created_at,id LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        return {"total": total, "items": [self.decode(row) for row in rows],
                "next_offset": offset + len(rows) if offset + len(rows) < total else None}

    def import_csv(self, csv_text: str, dry_run: bool = True, restore_export_escapes: bool = False) -> dict:
        if len(csv_text.encode("utf-8")) > 2_000_000:
            raise ValueError("CSVは2MB以内で指定してください。")
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
        if not reader.fieldnames:
            raise ValueError("CSVの見出しがありません。")
        headers = [ALIASES.get(h.strip(), h.strip()) for h in reader.fieldnames]
        if len(headers) != len(set(headers)):
            raise ValueError("CSVの見出しが重複しています。")
        if not {"question", "answer"}.issubset(headers):
            raise ValueError("CSVには question,answer または 問題文,答え の見出しが必要です。")
        if set(headers) - set(FIELDS):
            raise ValueError("未対応のCSV見出し: " + ", ".join(sorted(set(headers) - set(FIELDS))))
        reader.fieldnames = headers
        items = []
        for index, row in enumerate(reader, start=2):
            if len(items) >= 1000:
                raise ValueError("CSVは1回1000問以内で指定してください。")
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"CSV {index}行目の列数が見出しと一致しません。")
            # Only decode our export format when explicitly requested. Ordinary input is literal.
            if restore_export_escapes:
                row = {key: value[1:] if value.startswith("'") and (
                    value.startswith("''") or value[1:].lstrip().startswith(("=", "+", "-", "@")))
                    else value for key, value in row.items()}
            try:
                items.append(validate(row))
            except ValueError as error:
                raise ValueError(f"CSV {index}行目: {error}") from error
        inserted, skipped = [], []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A savepoint gives preview exactly the same duplicate/conflict behavior as commit.
            db.execute("SAVEPOINT csv_import")
            for index, item in enumerate(items, start=2):
                try:
                    result = self._insert(db, item)
                except ValueError as error:
                    raise ValueError(f"CSV {index}行目: {error}") from error
                (inserted if result["created"] else skipped).append(result["quiz"]["id"])
            if dry_run:
                db.execute("ROLLBACK TO csv_import")
            db.execute("RELEASE csv_import")
        return {"dry_run": dry_run, "rows": len(items), "new_count": len(inserted),
                "duplicate_count": len(skipped),
                "created_ids": [] if dry_run else inserted,
                "note": "プレビューです。まだ保存していません。" if dry_run else "ローカル問題帳に保存しました。"}

    def export_csv(self, status: str = "") -> dict:
        if status and status not in STATUSES:
            raise ValueError("状態は draft / ready / entered で指定してください。")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM quizzes" + (" WHERE status=?" if status else "") +
                              " ORDER BY created_at,id", (status,) if status else ()).fetchall()
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        escaped = 0
        for row in rows:
            item = self.decode(row)
            item["tags"] = "|".join(item["tags"])
            exported = {key: item[key] for key in FIELDS}
            for key, value in exported.items():
                if value.startswith("'") or value.lstrip().startswith(("=", "+", "-", "@")):
                    exported[key] = "'" + value
                    escaped += 1
            writer.writerow(exported)
        return {"count": len(rows), "csv": output.getvalue(), "escaped_cells": escaped,
                "note": "問題データのCSVです。みんはや公式の取り込み形式ではありません。復習履歴は含みません。再取込時はrestore_export_escapes=trueを指定してください。"}

    def registration_text(self, quiz_id: str) -> dict:
        item = self.get(quiz_id)
        return {"quiz_id": quiz_id, "question": item["question"], "answer": item["answer"],
                "reading": item["reading"], "explanation": item["explanation"],
                "source_url": item["source_url"], "status": item["status"],
                "note": "アプリ入力用データを用意しました。アプリへの送信・登録は行っていません。"}

    def record_review(self, quiz_id: str, correct: bool) -> dict:
        if not isinstance(correct, bool):
            raise ValueError("correct は true / false で指定してください。")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM quizzes WHERE id=?", (quiz_id,)).fetchone():
                raise ValueError("指定された問題IDがありません。")
            db.execute("INSERT INTO reviews(quiz_id,correct,reviewed_at) VALUES (?,?,?)",
                       (quiz_id, int(correct), now()))
            row = db.execute("""SELECT COUNT(*) attempts,SUM(correct) correct_count,
                COUNT(*)-SUM(correct) wrong_count FROM reviews WHERE quiz_id=?""", (quiz_id,)).fetchone()
        return {"quiz_id": quiz_id, **dict(row)}

    def review_queue(self, limit: int = 20) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("limit は1〜100で指定してください。")
        with self.connect() as db:
            rows = db.execute("""SELECT q.*,COUNT(r.id) attempts,
                COALESCE(SUM(1-r.correct),0) wrong_count,MAX(r.reviewed_at) last_reviewed_at
                FROM quizzes q LEFT JOIN reviews r ON r.quiz_id=q.id
                WHERE q.status IN ('ready','entered') GROUP BY q.id
                ORDER BY CASE WHEN COUNT(r.id)=0 THEN 0 ELSE 1 END,
                    1.0*COALESCE(SUM(1-r.correct),0)/MAX(COUNT(r.id),1) DESC,
                    last_reviewed_at ASC,q.created_at,q.id LIMIT ?""", (limit,)).fetchall()
        return {"items": [self.decode(row) for row in rows],
                "note": "ready/entered の問題から、未復習→誤答率が高い→前回の復習が古い順です。"}
