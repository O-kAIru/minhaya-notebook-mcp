import os
import json
import selectors
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minhaya_mcp.store import Notebook


class NotebookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "quizzes.sqlite3"
        self.store = Notebook(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, question="日本の首都はどこでしょう？", answer="東京", **kwargs):
        return self.store.create(question=question, answer=answer, **kwargs)["quiz"]

    def test_persistence_and_unicode_duplicate(self):
        first = self.create("ＡＢＣ とは？", "アルファベット", source_note="原本")
        result = self.store.create(question="abcとは？", answer="アルファベット", source_note="追加")
        self.assertFalse(result["created"])
        self.assertEqual(result["quiz"]["source_note"], "原本")
        self.assertEqual(Notebook(self.path).get(first["id"])["question"], "ＡＢＣ とは？")

    def test_conflict_does_not_replace_answer(self):
        first = self.create()
        with self.assertRaisesRegex(ValueError, "別の答え"):
            self.create(answer="大阪")
        self.assertEqual(self.store.get(first["id"])["answer"], "東京")

    def test_csv_preview_atomicity_and_batch_duplicates(self):
        csv = "問題文,答え\nQ1,A1\nQ1,A1\nQ2,A2\n"
        preview = self.store.import_csv(csv)
        self.assertEqual((preview["new_count"], preview["duplicate_count"]), (2, 1))
        self.assertEqual(self.store.search()["total"], 0)
        committed = self.store.import_csv(csv, dry_run=False)
        self.assertEqual(len(committed["created_ids"]), 2)
        self.assertEqual(self.store.search()["total"], 2)

    def test_csv_conflict_rolls_back_the_whole_import(self):
        self.create("Q0", "A0")
        with self.assertRaisesRegex(ValueError, "別の答え"):
            self.store.import_csv("question,answer\nQ1,A1\nQ0,WRONG\n", dry_run=False)
        self.assertEqual(self.store.search()["total"], 1)

    def test_csv_invalid_row_or_header_does_not_write(self):
        for csv in ["question,answer\nQ1,A1\nQ2,\n", "question,answer\nQ1,A1,extra\n",
                    "question,answer\nQ1\n", "question,問題文,answer\nQ,Q,A\n",
                    "question,answer,typo\nQ,A,x\n"]:
            with self.subTest(csv=csv):
                with self.assertRaises(ValueError):
                    self.store.import_csv(csv, dry_run=False)
                self.assertEqual(self.store.search()["total"], 0)

    def test_csv_bom_quotes_newlines_and_formula_safe_roundtrip(self):
        first = self.create('"引用", 改行\n次の行', "=1+1", tags=["数学", "自作"],
                            explanation="改行\n説明", source_note="'=元メモ")
        exported = self.store.export_csv()
        self.assertEqual(exported["escaped_cells"], 2)
        target = Notebook(Path(self.temp.name) / "other.sqlite3")
        target.import_csv("\ufeff" + exported["csv"], dry_run=False, restore_export_escapes=True)
        imported = target.search()["items"][0]
        for key in ["question", "answer", "tags", "explanation", "source_note"]:
            self.assertEqual(imported[key], first[key])

    def test_ordinary_csv_keeps_literal_apostrophe(self):
        self.store.import_csv("question,answer\nQ,'=literal\n", dry_run=False)
        self.assertEqual(self.store.search()["items"][0]["answer"], "'=literal")

    def test_search_filters_and_pagination(self):
        self.create("Q1", "ABC", category="数学", tags=["数", "練習"], status="ready")
        self.create("Q2", "DEF", category="数学", tags=["数列"])
        self.create("Q3", "GHI", category="歴史", tags=["数"])
        self.assertEqual(self.store.search("ａｂｃ", category="数学", tag="数", status="ready")["total"], 1)
        self.assertEqual(self.store.search(tag="数")["total"], 2)
        first = self.store.search(limit=2)
        second = self.store.search(limit=2, offset=first["next_offset"])
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_offset"])
        self.assertEqual(self.store.export_csv()["count"], 3)

    def test_update_preserves_other_fields_and_history(self):
        first = self.create(source_note="もとの出典", tags=["自作"])
        self.store.record_review(first["id"], False)
        changed = self.store.update(first["id"], reading="とうきょう", status="ready")
        self.assertEqual(changed["tags"], ["自作"])
        self.assertEqual(changed["source_note"], "もとの出典")
        self.assertEqual(changed["created_at"], first["created_at"])
        self.assertEqual(self.store.review_queue()["items"][0]["wrong_count"], 1)

    def test_update_conflict_keeps_original(self):
        a = self.create("Q1", "A1")
        b = self.create("Q2", "A2")
        with self.assertRaises(ValueError):
            self.store.update(b["id"], question="Q1")
        self.assertEqual(self.store.get(b["id"])["question"], "Q2")

    def test_review_order_and_draft_exclusion(self):
        draft = self.create("Q0", "A0")
        good = self.create("Q1", "A1", status="ready")
        weak = self.create("Q2", "A2", status="entered")
        unseen = self.create("Q3", "A3", status="ready")
        self.store.record_review(good["id"], True)
        self.store.record_review(weak["id"], False)
        queue = self.store.review_queue()["items"]
        self.assertEqual([item["id"] for item in queue], [unseen["id"], weak["id"], good["id"]])
        result = self.store.record_review(weak["id"], True)
        self.assertEqual((result["attempts"], result["correct_count"], result["wrong_count"]), (2, 1, 1))

    def test_invalid_values_and_unknown_id(self):
        for values in [{"question": ""}, {"source_url": "javascript:alert(1)"},
                       {"tags": [1]}, {"tags": ["x|y"]}, {"status": "typo"}]:
            with self.assertRaises(ValueError):
                self.create(**values)
        with self.assertRaises(ValueError):
            self.store.record_review("missing", True)
        with self.assertRaises(ValueError):
            self.store.search(limit=0)

    def test_registration_only_returns_text(self):
        first = self.create()
        result = self.store.registration_text(first["id"])
        self.assertIn("登録は行っていません", result["note"])
        self.assertEqual(self.store.get(first["id"])["status"], "draft")


class StdioIntegrationTests(unittest.TestCase):
    def test_initialize_discover_and_use_tools_over_real_stdio(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "MINHAYA_DB_PATH": str(Path(tmp) / "mcp.sqlite3")}
            root = Path(__file__).resolve().parent.parent
            process = subprocess.Popen([sys.executable, "-m", "minhaya_mcp.server"],
                                       cwd=root, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            sequence = 0

            def request(method, params=None):
                nonlocal sequence
                sequence += 1
                payload = {"jsonrpc": "2.0", "id": sequence, "method": method}
                if params is not None:
                    payload["params"] = params
                process.stdin.write((json.dumps(payload) + "\n").encode())
                process.stdin.flush()
                for _ in range(30):
                    if not selector.select(15):
                        self.fail(f"MCP response timeout for {method}")
                    line = process.stdout.readline()
                    if not line:
                        self.fail("MCP server exited: " + process.stderr.read().decode())
                    response = json.loads(line)
                    if response.get("id") == sequence:
                        self.assertNotIn("error", response, response)
                        return response["result"]
                self.fail("No matching response")

            try:
                initialized = request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "minhaya-test", "version": "0.1.0"}})
                self.assertIn("tools", initialized["capabilities"])
                process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                process.stdin.flush()
                tools = request("tools/list")["tools"]
                self.assertEqual(len(tools), 8)
                self.assertIn("create_quiz", [tool["name"] for tool in tools])
                prompts = request("prompts/list")["prompts"]
                self.assertEqual(prompts[0]["name"], "draft_quizzes")
                prompt = request("prompts/get", {"name": "draft_quizzes", "arguments": {"topic": "歴史"}})
                self.assertIn("draft", prompt["messages"][0]["content"]["text"])

                def call(name, arguments):
                    result = request("tools/call", {"name": name, "arguments": arguments})
                    self.assertFalse(result.get("isError", False), result)
                    return json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))

                created = call("create_quiz", {"question": "1時間は何分？", "answer": "60分", "tags": ["単位"]})
                quiz_id = created["quiz"]["id"]
                self.assertEqual(call("search_quizzes", {"tag": "単位"})["total"], 1)
                call("update_quiz", {"quiz_id": quiz_id, "status": "ready"})
                self.assertEqual(call("get_review_queue", {})["items"][0]["id"], quiz_id)
                self.assertEqual(call("record_review", {"quiz_id": quiz_id, "correct": False})["wrong_count"], 1)
                self.assertIn("登録は行っていません", call("get_registration_text", {"quiz_id": quiz_id})["note"])
                self.assertEqual(call("export_quizzes_csv", {})["count"], 1)
                preview = call("import_quizzes_csv", {"csv_text": "question,answer\nQ2,A2\n"})
                self.assertTrue(preview["dry_run"])
                invalid = request("tools/call", {"name": "create_quiz", "arguments": {"question": "", "answer": "A"}})
                self.assertTrue(invalid["isError"])
            finally:
                selector.close()
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stdout.close()
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
