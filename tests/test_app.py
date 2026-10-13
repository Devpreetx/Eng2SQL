"""
Backend tests — no LLM or network needed.

Run from the project root:

    python -m unittest discover -s tests -v

Everything runs inside a throw-away ENG2SQL_HOME, so the real uploads/,
backups/ and data/ folders are never touched.
"""

import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="eng2sql-test-")
os.environ["ENG2SQL_HOME"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app import agent, auth, config, db_utils, llm, main, rag, registry  # noqa: E402

config.MAX_UPLOAD_MB = 1

# Throw-away accounts that only ever exist inside the temporary test folder.
ALICE = {"name": "Alice Tester", "email": "alice@example.test", "password": "test-only-alice-pw"}
BOB = {"name": "Bob Tester", "email": "bob@example.test", "password": "test-only-bob-pw"}


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def sample_db_bytes() -> bytes:
    path = os.path.join(_TMP, "seed.sqlite")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            amount REAL,
            note TEXT
        );
        INSERT INTO customers VALUES (1, 'Asha', 'Delhi'), (2, 'Ben', 'Pune'), (3, 'Chen', 'Delhi');
        INSERT INTO orders VALUES (1, 1, 120.5, 'a;b'), (2, 1, 80, NULL), (3, 2, 42, 'x');
        """
    )
    conn.commit()
    conn.close()
    with open(path, "rb") as f:
        return f.read()


class ValidationTests(unittest.TestCase):
    known = {"customers", "orders"}

    def check(self, sql):
        return db_utils.validate_statements([sql], self.known)[0][2]

    def test_reads_are_classified_as_select(self):
        self.assertEqual(self.check("SELECT * FROM customers"), "select")
        self.assertEqual(self.check("SELECT id FROM customers UNION SELECT id FROM orders"), "select")
        self.assertEqual(
            self.check("WITH big AS (SELECT * FROM orders WHERE amount > 50) SELECT * FROM big"),
            "select",
        )

    def test_keywords_inside_string_literals_are_allowed(self):
        self.assertEqual(self.check("SELECT * FROM orders WHERE note = 'please DROP it'"), "select")

    def test_dangerous_statements_are_rejected(self):
        for sql in (
            "DROP TABLE customers",
            "DROP\nTABLE customers",
            "ALTER TABLE customers ADD COLUMN x TEXT",
            "PRAGMA table_info(customers)",
            "ATTACH DATABASE 'x.db' AS x",
            "VACUUM",
            "REPLACE INTO customers VALUES (1, 'a', 'b')",
            "CREATE TRIGGER t AFTER INSERT ON customers BEGIN DELETE FROM orders; END",
            "DELETE FROM customers",
            "UPDATE customers SET name = 'x'",
            "SELECT * FROM secrets",
        ):
            with self.assertRaises(ValueError, msg=sql):
                self.check(sql)

    def test_table_created_earlier_in_batch_is_in_scope(self):
        validated = db_utils.validate_statements(
            ["CREATE TABLE tags (id INTEGER, label TEXT)", "INSERT INTO tags VALUES (1, 'a')"],
            self.known,
        )
        self.assertEqual([v[2] for v in validated], ["create", "insert"])

    def test_split_ignores_semicolons_in_literals_and_comments(self):
        parts = db_utils.split_statements(
            "INSERT INTO orders VALUES (9, 1, 1, 'a;b'); -- note; here\nSELECT 1;;"
        )
        self.assertEqual(len(parts), 2)
        self.assertIn("'a;b'", parts[0])


class RagTests(unittest.TestCase):
    def test_small_schema_is_returned_whole_without_a_vector_store(self):
        schema = {"t1": {"columns": [{"name": "id", "type": "INTEGER", "primary_key": True}]}}
        result = rag.retrieve_schema("db-x", "anything", schema=schema)
        self.assertEqual(result["tables"], ["t1"])
        self.assertIn("id INTEGER PRIMARY KEY", result["documents"][0]["content"])
        self.assertEqual(rag.index_schema("db-x", schema)["skipped"], True)
        self.assertIsNone(rag._vectorstore)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._ctx = TestClient(main.app)
        cls.client = cls._ctx.__enter__()
        signup = cls.client.post("/auth/signup", json=ALICE)
        assert signup.status_code == 200, signup.text
        res = cls.client.post(
            "/upload", files={"file": ("shop.sqlite", sample_db_bytes(), "application/octet-stream")}
        )
        assert res.status_code == 200, res.text
        cls.db_id = res.json()["db_id"]

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def sql(self, text):
        return self.sql_on(self.db_id, text)

    def sql_on(self, db_id, text, confirm=False):
        res = self.client.post("/sql/execute", json={"db_id": db_id, "sql": text})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        if confirm:
            res = self.client.post(
                "/confirm", json={"db_id": db_id, "query_id": data["query_id"], "confirm": True}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["status"], "executed", data)
        return data

    def test_health_and_security_headers(self):
        res = self.client.get("/health")
        self.assertEqual(res.json()["status"], "ok")
        self.assertEqual(res.headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'self'", res.headers["content-security-policy"])
        self.assertNotIn("access-control-allow-origin", res.headers)

    def test_api_requires_sign_in(self):
        anonymous = TestClient(main.app)
        self.assertEqual(anonymous.get("/databases").status_code, 401)
        self.assertEqual(anonymous.get(f"/schema/{self.db_id}").status_code, 401)
        self.assertEqual(
            anonymous.post("/sql/execute", json={"db_id": self.db_id, "sql": "SELECT 1"}).status_code,
            401,
        )
        self.assertEqual(anonymous.get("/health").status_code, 200)
        self.assertFalse(anonymous.get("/auth/status").json()["authenticated"])

    def test_cross_site_writes_are_blocked(self):
        res = self.client.post(
            "/sql/execute",
            json={"db_id": self.db_id, "sql": "SELECT 1"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(res.status_code, 403)

    def test_users_cannot_reach_each_others_databases(self):
        bob = TestClient(main.app)
        self.assertEqual(bob.post("/auth/signup", json=BOB).status_code, 200)
        self.assertEqual(bob.get("/databases").json()["databases"], [])
        self.assertEqual(bob.get(f"/schema/{self.db_id}").status_code, 404)
        self.assertEqual(bob.get(f"/table/{self.db_id}/customers").status_code, 404)
        self.assertEqual(bob.get(f"/download/{self.db_id}").status_code, 404)
        self.assertEqual(bob.delete(f"/database/{self.db_id}").status_code, 404)

        self.sql("SELECT 'bob-must-not-see' AS x FROM customers")
        mine = self.client.get(f"/history/{self.db_id}?search=bob-must-not-see").json()["items"]
        self.assertEqual(bob.get(f"/history/detail/{mine[0]['history_id']}").status_code, 404)

    def test_sign_in_sign_out_and_password_change(self):
        client = TestClient(main.app)
        carol = {"name": "Carol", "email": "carol@example.test", "password": "test-only-carol-pw"}
        self.assertEqual(client.post("/auth/signup", json=carol).status_code, 200)
        self.assertEqual(client.post("/auth/signup", json=carol).status_code, 409)
        self.assertEqual(
            client.post("/auth/signup", json={**carol, "email": "c2@example.test", "password": "short"}).status_code,
            400,
        )

        cookie = client.cookies.get(auth.COOKIE_NAME)
        self.assertTrue(cookie)
        with auth.closing(auth._connect()) as conn:
            stored = conn.execute("SELECT password_hash FROM users WHERE email = ?", (carol["email"],)).fetchone()[0]
            tokens = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn(carol["password"], stored)
        self.assertNotIn(cookie, tokens)  # only a hash of the session token is stored

        self.assertEqual(client.post("/auth/signout").status_code, 200)
        self.assertEqual(client.get("/databases").status_code, 401)

        wrong = client.post("/auth/signin", json={"email": carol["email"], "password": "not-the-password"})
        self.assertEqual(wrong.status_code, 401)
        unknown = client.post("/auth/signin", json={"email": "nobody@example.test", "password": "whatever-pw"})
        self.assertEqual((unknown.status_code, unknown.json()), (wrong.status_code, wrong.json()))

        self.assertEqual(client.post("/auth/signin", json={"email": "CAROL@example.test", "password": carol["password"]}).status_code, 200)
        self.assertEqual(client.get("/auth/status").json()["user"]["name"], "Carol")

        bad_change = client.post("/auth/password", json={"current_password": "nope-nope", "new_password": "test-only-carol-new"})
        self.assertEqual(bad_change.status_code, 400)
        changed = client.post("/auth/password", json={"current_password": carol["password"], "new_password": "test-only-carol-new"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(client.get("/databases").status_code, 200)  # this session survives
        self.assertEqual(
            TestClient(main.app).post("/auth/signin", json={"email": carol["email"], "password": carol["password"]}).status_code,
            401,
        )

    def test_repeated_failed_sign_ins_are_locked_out(self):
        client = TestClient(main.app)
        attempt = {"email": "alice@example.test", "password": "wrong-password"}
        codes = [client.post("/auth/signin", json=attempt).status_code for _ in range(auth.MAX_FAILED_SIGNINS + 1)]
        self.assertEqual(codes, [401] * auth.MAX_FAILED_SIGNINS + [429])
        auth._failures.clear()

    def test_signup_can_be_closed(self):
        config.ALLOW_SIGNUP = False
        try:
            res = TestClient(main.app).post(
                "/auth/signup", json={"name": "Dan", "email": "dan@example.test", "password": "test-only-dan-pw"}
            )
        finally:
            config.ALLOW_SIGNUP = True
        self.assertEqual(res.status_code, 403)

    def test_rename_and_delete_database(self):
        db_id = self.client.post("/database/create", params={"name": "temp"}).json()["db_id"]
        self.sql_on(db_id, "CREATE TABLE t (id INTEGER)", confirm=True)

        renamed = self.client.patch(f"/database/{db_id}", json={"name": "  Renamed DB  "})
        self.assertEqual(renamed.json()["name"], "Renamed DB")
        self.assertEqual(self.client.patch(f"/database/{db_id}", json={"name": "   "}).status_code, 400)

        path = main.DBS[db_id]["path"]
        self.assertTrue(db_utils.list_backups(path, config.BACKUP_DIR))
        self.assertEqual(self.client.delete(f"/database/{db_id}").status_code, 200)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(db_utils.list_backups(path, config.BACKUP_DIR), [])
        self.assertEqual(self.client.get(f"/schema/{db_id}").status_code, 404)
        self.assertNotIn(db_id, [r["db_id"] for r in registry.list_registered()])

    def test_backup_restore_undoes_a_write(self):
        db_id = self.client.post("/database/create", params={"name": "undo"}).json()["db_id"]
        self.sql_on(db_id, "CREATE TABLE notes (body TEXT); INSERT INTO notes VALUES ('keep')", confirm=True)
        done = self.sql_on(db_id, "DELETE FROM notes WHERE body = 'keep'", confirm=True)
        self.assertEqual(self.client.get(f"/table/{db_id}/notes").json()["total_rows"], 0)

        backups = self.client.get(f"/backups/{db_id}").json()["backups"]
        self.assertEqual(len(backups), 2)
        self.assertEqual(backups[0]["name"], os.path.basename(done["backup"]))  # newest first

        bad = self.client.post(f"/backups/{db_id}/restore", json={"name": "../../etc/passwd"})
        self.assertEqual(bad.status_code, 404)
        other = db_utils.list_backups(main.DBS[self.db_id]["path"], config.BACKUP_DIR)
        if other:  # another database's backup must not be restorable here
            foreign = self.client.post(f"/backups/{db_id}/restore", json={"name": other[0]["name"]})
            self.assertEqual(foreign.status_code, 404)

        restored = self.client.post(f"/backups/{db_id}/restore", json={"name": backups[0]["name"]})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.client.get(f"/table/{db_id}/notes").json()["total_rows"], 1)
        self.assertEqual(len(self.client.get(f"/backups/{db_id}").json()["backups"]), 3)

    def test_old_backups_are_pruned(self):
        db_id = self.client.post("/database/create", params={"name": "prune"}).json()["db_id"]
        path = main.DBS[db_id]["path"]
        original, db_utils.MAX_BACKUPS_PER_DB = db_utils.MAX_BACKUPS_PER_DB, 3
        try:
            for _ in range(5):
                db_utils.backup_db(path, config.BACKUP_DIR)
        finally:
            db_utils.MAX_BACKUPS_PER_DB = original
        self.assertEqual(len(db_utils.list_backups(path, config.BACKUP_DIR)), 3)

    def test_starred_history_and_sql_formatting(self):
        self.sql("SELECT 'star-me' AS marker FROM customers")
        item = self.client.get(f"/history/{self.db_id}?search=star-me").json()["items"][0]
        self.assertFalse(item["starred"])
        self.client.post(f"/history/star/{item['history_id']}", json={"starred": True})
        starred = self.client.get(f"/history/{self.db_id}?starred=true").json()["items"]
        self.assertEqual([i["history_id"] for i in starred], [item["history_id"]])

        formatted = self.client.post("/sql/format", json={"sql": "select a,b from t where x=1; select 2"})
        self.assertEqual(formatted.status_code, 200)
        self.assertIn("SELECT\n  a,\n  b\nFROM t", formatted.json()["sql"])
        self.assertEqual(self.client.post("/sql/format", json={"sql": "select ((("}).status_code, 400)

    def test_static_ui_is_served(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Eng2SQL", res.text)

    def test_upload_lists_tables_and_registers_database(self):
        dbs = self.client.get("/databases").json()["databases"]
        mine = next(d for d in dbs if d["db_id"] == self.db_id)
        self.assertEqual(mine["tables"], ["customers", "orders"])
        self.assertIn(self.db_id, [r["db_id"] for r in registry.list_registered()])

    def test_upload_rejects_bad_files(self):
        bad_type = self.client.post("/upload", files={"file": ("notes.txt", b"hi", "text/plain")})
        self.assertEqual(bad_type.status_code, 400)
        not_sqlite = self.client.post("/upload", files={"file": ("fake.db", b"not a database", "x/y")})
        self.assertEqual(not_sqlite.status_code, 400)
        too_big = self.client.post(
            "/upload", files={"file": ("big.csv", b"a,b\n" + b"1,2\n" * 400_000, "text/csv")}
        )
        self.assertEqual(too_big.status_code, 413)
        leftovers = [f for f in os.listdir(config.UPLOAD_DIR) if not f.startswith(self.db_id)]
        self.assertEqual([f for f in leftovers if f.endswith(".upload")], [])

    def test_csv_upload_becomes_a_queryable_table(self):
        res = self.client.post(
            "/upload", files={"file": ("sales 2024-q1.csv", b"region,total\nN,10\nS,20\n", "text/csv")}
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["tables"], ["sales_2024_q1"])

    def test_select_executes_immediately(self):
        data = self.sql("SELECT name FROM customers WHERE city = 'Delhi' ORDER BY id;")
        self.assertEqual(data["status"], "executed")
        self.assertEqual([r["name"] for r in data["results"][0]["rows"]], ["Asha", "Chen"])
        self.assertFalse(data["results"][0]["truncated"])

    def test_blocked_statement_is_rejected(self):
        data = self.sql("DROP TABLE customers")
        self.assertEqual(data["status"], "rejected")
        self.assertIn("DROP", data["reason"])

    def test_write_requires_confirmation_then_runs_with_backup(self):
        data = self.sql("UPDATE customers SET city = 'Mumbai' WHERE id = 2")
        self.assertEqual(data["status"], "confirmation_required")
        self.assertEqual(data["previews"][0]["affected_count"], 1)

        untouched = self.sql("SELECT city FROM customers WHERE id = 2")
        self.assertEqual(untouched["results"][0]["rows"][0]["city"], "Pune")

        res = self.client.post(
            "/confirm", json={"db_id": self.db_id, "query_id": data["query_id"], "confirm": True}
        ).json()
        self.assertEqual(res["status"], "executed")
        self.assertTrue(res["backup"].startswith("backups/"))
        self.assertTrue(os.path.exists(os.path.join(config.BACKUP_DIR, os.path.basename(res["backup"]))))

        changed = self.sql("SELECT city FROM customers WHERE id = 2")
        self.assertEqual(changed["results"][0]["rows"][0]["city"], "Mumbai")

        again = self.client.post(
            "/confirm", json={"db_id": self.db_id, "query_id": data["query_id"], "confirm": True}
        )
        self.assertEqual(again.status_code, 404)

    def test_cancelled_write_changes_nothing_and_updates_history(self):
        data = self.sql("DELETE FROM orders WHERE id = 3")
        res = self.client.post(
            "/confirm", json={"db_id": self.db_id, "query_id": data["query_id"], "confirm": False}
        ).json()
        self.assertEqual(res["status"], "cancelled")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM orders")["results"][0]["rows"][0]["n"], 3)

        items = self.client.get(f"/history/{self.db_id}?status=cancelled").json()["items"]
        self.assertTrue(any("DELETE FROM orders" in " ".join(i["sql"]) for i in items))

    def test_history_search_pagination_and_detail(self):
        self.sql("SELECT 'needle-xyz' AS marker FROM customers")
        page = self.client.get(f"/history/{self.db_id}?search=needle-xyz&limit=1").json()
        self.assertEqual(page["total"], 1)
        detail = self.client.get(f"/history/detail/{page['items'][0]['history_id']}").json()
        self.assertEqual(detail["status"], "executed")
        self.assertEqual(self.client.get("/history/detail/nope").status_code, 404)

    def test_table_preview_and_exports(self):
        page = self.client.get(f"/table/{self.db_id}/customers?page=1&limit=2").json()
        self.assertEqual((page["total_rows"], len(page["rows"])), (3, 2))
        self.assertEqual(page["columns"], ["id", "name", "city"])
        self.assertEqual(self.client.get(f"/table/{self.db_id}/missing").status_code, 404)

        csv_res = self.client.get(f"/export/table/{self.db_id}/customers?format=csv")
        self.assertEqual(csv_res.status_code, 200)
        self.assertIn("id,name,city", csv_res.content.decode("utf-8-sig"))

        xlsx_res = self.client.post(
            "/export/query",
            json={"db_id": self.db_id, "sql": "SELECT * FROM orders", "format": "xlsx"},
        )
        self.assertEqual(xlsx_res.status_code, 200)
        self.assertEqual(xlsx_res.content[:2], b"PK")

        write = self.client.post(
            "/export/query",
            json={"db_id": self.db_id, "sql": "DELETE FROM orders WHERE id = 1", "format": "csv"},
        )
        self.assertEqual(write.status_code, 400)

    def test_create_database_table_and_import(self):
        created = self.client.post("/database/create", params={"name": "scratch pad!"}).json()
        self.assertEqual(created["name"], "scratchpad.db")
        db_id = created["db_id"]

        table = self.client.post(
            "/table/create",
            json={
                "db_id": db_id,
                "table_name": "people",
                "columns": [{"name": "id", "type": "INTEGER"}, {"name": "name", "type": "TEXT"}],
            },
        )
        self.assertEqual(table.status_code, 200, table.text)
        self.assertIn("people", table.json()["schema"])

        duplicate = self.client.post(
            "/table/create",
            json={"db_id": db_id, "table_name": "people", "columns": [{"name": "id"}]},
        )
        self.assertEqual(duplicate.status_code, 400)

        def do_import(mode, table_name="people"):
            return self.client.post(
                "/import/table",
                data={"db_id": db_id, "table_name": table_name, "mode": mode},
                files={"file": ("p.csv", io.BytesIO(b"id,name\n1,Ann\n2,Raj\n"), "text/csv")},
            )

        self.assertEqual(do_import("new").status_code, 400)  # table already exists
        self.assertEqual(do_import("append", "bad name;").status_code, 400)
        appended = do_import("append")
        self.assertEqual(appended.status_code, 200, appended.text)
        self.assertEqual(appended.json()["rows"], 2)
        self.assertEqual(self.client.get(f"/table/{db_id}/people").json()["total_rows"], 2)

    def test_databases_survive_a_restart(self):
        main.DBS.pop(self.db_id)
        main._load_registered_databases()
        self.assertIn(self.db_id, main.DBS)
        self.assertEqual(main.DBS[self.db_id]["name"], "shop.sqlite")


class AgentTests(unittest.TestCase):
    """The LangGraph flow end to end, with the LLM replaced by a stub."""

    def setUp(self):
        self.path = os.path.join(_TMP, "agent.sqlite")
        with open(self.path, "wb") as f:
            f.write(sample_db_bytes())
        self.schema = db_utils.extract_schema(self.path)
        self.calls = {"generate": 0, "retrieved": []}

        self._generate, self._invoke = llm.generate_sql, llm.invoke_text
        llm.invoke_text = lambda prompt: "Stub answer."
        self.addCleanup(setattr, llm, "generate_sql", self._generate)
        self.addCleanup(setattr, llm, "invoke_text", self._invoke)

    def stub_generate(self, *responses):
        queue = list(responses)

        def fake(db_id, question, schema=None, retrieved=None):
            self.calls["generate"] += 1
            self.calls["retrieved"].append(retrieved)
            return {
                "statements": queue.pop(0),
                "intent": "read",
                "target_table": "",
                "explanation": "stub",
                "needs_clarification": False,
                "clarification_question": "",
            }

        llm.generate_sql = fake

    def run_agent(self, question, session="s1"):
        return agent.run_agent("agent-db", question, self.schema, self.path, session)

    def test_read_returns_answer_chart_and_reuses_retrieval(self):
        self.stub_generate(["SELECT city, COUNT(*) AS n FROM customers GROUP BY city"])
        result = self.run_agent("customers per city")
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["explanation"], "Stub answer.")
        self.assertEqual(result["chart"]["chart_type"], "bar")
        self.assertEqual(self.calls["retrieved"][0]["tables"], ["customers", "orders"])

    def test_invalid_sql_is_corrected(self):
        self.stub_generate(["SELECT * FROM nowhere"], ["SELECT * FROM orders"])
        result = self.run_agent("orders", session="s2")
        self.assertEqual(result["status"], "executed")
        self.assertEqual(self.calls["generate"], 2)

    def test_write_waits_for_confirmation(self):
        self.stub_generate(["DELETE FROM orders WHERE id = 1"])
        result = self.run_agent("delete order 1", session="s3")
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(result["previews"][0]["affected_count"], 1)

    def test_no_statements_is_reported_as_an_error(self):
        self.stub_generate([])
        result = self.run_agent("???", session="s4")
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
