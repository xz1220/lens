#!/usr/bin/env python3
"""lens summarizer test suite — stdlib unittest, zero network, zero 真 LLM。

LLM 一律注入 fake runner（按 prompt 里的编号回 JSON）；DB 用临时文件。
锁两条不变量：
  1. summarize 只写 ai_* / content_text，绝不碰 score/tags/status/comment。
  2. 一条/一批失败只丢那一条，不传染、不写半截结果。

Run: python3 -m unittest discover -s tests -v
"""
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import summarize  # noqa: E402

# 旧库 schema（没有 ai_* 列）——ensure_ai_columns 的迁移对象
LEGACY_SCHEMA = """
CREATE TABLE items (
  id TEXT PRIMARY KEY, source_key TEXT NOT NULL, source_name TEXT, tier TEXT,
  category TEXT, title TEXT, url TEXT, summary TEXT, author TEXT,
  published_at TEXT, fetched_at TEXT NOT NULL,
  score INTEGER, tags TEXT, status TEXT NOT NULL DEFAULT 'captured',
  comment TEXT, triaged_at TEXT
);
"""


def item_columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(items)")}


class TmpDbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="lens-sumtest-")
        self.db_path = Path(self._tmp) / "lens.db"
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def legacy_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(LEGACY_SCHEMA)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def fresh_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.executescript((ROOT / "schema.sql").read_text())
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def insert_item(conn, iid, *, source_key="s", tier="P0", title="t",
                    url="https://x/1", summary="", status="captured",
                    published_at=None, fetched_at="2026-06-09T00:00:00+00:00"):
        conn.execute(
            "INSERT INTO items (id, source_key, tier, title, url, summary, status,"
            " published_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (iid, source_key, tier, title, url, summary, status, published_at, fetched_at),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# 迁移
# --------------------------------------------------------------------------- #
class TestEnsureAiColumns(TmpDbTestCase):
    def test_legacy_db_gets_all_ai_columns(self):
        conn = self.legacy_conn()
        added = summarize.ensure_ai_columns(conn)
        self.assertEqual(set(added), set(summarize.AI_COLUMNS))
        self.assertLessEqual(set(summarize.AI_COLUMNS), item_columns(conn))

    def test_migration_idempotent(self):
        conn = self.legacy_conn()
        summarize.ensure_ai_columns(conn)
        self.assertEqual(summarize.ensure_ai_columns(conn), [])  # second run no-op

    def test_fresh_schema_needs_no_migration(self):
        conn = self.fresh_conn()
        self.assertEqual(summarize.ensure_ai_columns(conn), [])

    def test_duplicate_column_race_tolerated(self):
        # check-then-ALTER 窗口：另一进程已加列、本进程的 PRAGMA 却没看到。
        # 用代理 conn 谎报「无列」复现竞态——ALTER 撞 duplicate column 必须
        # 被吞掉（视为迁移已完成），不许崩。
        real = self.fresh_conn()  # 列已齐全

        class RacyConn:
            def execute(self, sql, *a):
                if sql.startswith("PRAGMA"):
                    return iter([])          # 谎称一列都没有
                return real.execute(sql, *a)  # ALTER 将撞已存在列

            def commit(self):
                real.commit()

        added = summarize.ensure_ai_columns(RacyConn())
        self.assertEqual(added, [])  # 全部 duplicate，全部良性吞掉


# --------------------------------------------------------------------------- #
# 选条
# --------------------------------------------------------------------------- #
class TestPickItems(TmpDbTestCase):
    def test_excludes_ignored_and_already_summarized(self):
        conn = self.fresh_conn()
        self.insert_item(conn, "a", title="fresh")
        self.insert_item(conn, "b", url="https://x/2", status="ignored")
        self.insert_item(conn, "c", url="https://x/3")
        conn.execute("UPDATE items SET ai_summarized_at='2026-06-09T00:00:00+00:00' WHERE id='c'")
        conn.commit()
        picked = {r["id"] for r in summarize.pick_items(conn, None)}
        self.assertEqual(picked, {"a"})

    def test_tier_rank_then_recency(self):
        conn = self.fresh_conn()
        self.insert_item(conn, "hot-new", tier="heat", url="https://x/1",
                         published_at="2026-06-09T00:00:00+00:00")
        self.insert_item(conn, "p0-old", tier="P0", url="https://x/2",
                         published_at="2026-01-01T00:00:00+00:00")
        self.insert_item(conn, "p0-new", tier="P0", url="https://x/3",
                         published_at="2026-06-08T00:00:00+00:00")
        ids = [r["id"] for r in summarize.pick_items(conn, None)]
        self.assertEqual(ids, ["p0-new", "p0-old", "hot-new"])

    def test_limit_and_source_filter(self):
        conn = self.fresh_conn()
        for i in range(5):
            self.insert_item(conn, f"a{i}", source_key="src-a", url=f"https://a/{i}")
        self.insert_item(conn, "b0", source_key="src-b", url="https://b/0")
        self.assertEqual(len(summarize.pick_items(conn, 3)), 3)
        only_b = summarize.pick_items(conn, None, source="src-b")
        self.assertEqual([r["id"] for r in only_b], ["b0"])

    def test_exclude_sources_skipped_unless_explicit(self):
        conn = self.fresh_conn()
        self.insert_item(conn, "noise", source_key="firehose", url="https://f/1")
        self.insert_item(conn, "signal", source_key="blog", url="https://b/1")
        picked = {r["id"] for r in summarize.pick_items(conn, None, exclude_sources={"firehose"})}
        self.assertEqual(picked, {"signal"})
        # 显式 --source 时用户说了算，digest: false 不再拦
        forced = summarize.pick_items(conn, None, source="firehose", exclude_sources={"firehose"})
        self.assertEqual([r["id"] for r in forced], ["noise"])

    def test_no_digest_sources_reads_yaml(self):
        # sources.yml 里 sec-latest-filings 标了 digest: false（全量申报流水）
        skip = summarize.no_digest_sources()
        self.assertIn("sec-latest-filings", skip)
        self.assertNotIn("sec-edgar-api", skip)  # 定向 AI filing 仍要总结


# --------------------------------------------------------------------------- #
# 正文提取（纯函数）
# --------------------------------------------------------------------------- #
class TestExtractMainText(unittest.TestCase):
    def test_prefers_article_over_page_chrome(self):
        raw = ("<html><body><nav>Menu Home About</nav>"
               "<article><h1>Real Title</h1><p>First para.</p><p>Second.</p></article>"
               "<footer>copyright</footer></body></html>")
        out = summarize.extract_main_text(raw)
        self.assertIn("Real Title", out)
        self.assertIn("First para.", out)
        self.assertNotIn("Menu Home", out)
        self.assertNotIn("copyright", out)

    def test_strips_script_style_blocks(self):
        raw = "<main><style>h1 { color: red; }</style><script>var x=1;</script><p>Body text here.</p></main>"
        out = summarize.extract_main_text(raw)
        self.assertIn("Body text here.", out)
        self.assertNotIn("color", out)
        self.assertNotIn("var x", out)

    def test_no_article_falls_back_to_body_minus_chrome(self):
        raw = "<body><header>logo</header><div><p>Loose paragraph.</p></div><aside>ads</aside></body>"
        out = summarize.extract_main_text(raw)
        self.assertIn("Loose paragraph.", out)
        self.assertNotIn("logo", out)
        self.assertNotIn("ads", out)

    def test_block_tags_become_newlines_and_entities_decode(self):
        out = summarize.extract_main_text("<main><p>a &amp; b</p><li>item</li></main>")
        self.assertIn("a & b", out)
        self.assertIn("\n", out)

    def test_bytes_garbage_and_empty(self):
        self.assertEqual(summarize.extract_main_text(b""), "")
        self.assertEqual(summarize.extract_main_text(None), "")
        self.assertIn("ok", summarize.extract_main_text(b"<p>ok</p>"))

    def test_limit_applied(self):
        out = summarize.extract_main_text("<p>" + "x" * 9000 + "</p>", limit=100)
        self.assertLessEqual(len(out), 100)


# --------------------------------------------------------------------------- #
# SSRF 闸（IP 字面量不触发 DNS 查询，零网络）
# --------------------------------------------------------------------------- #
class TestUrlIsPublic(unittest.TestCase):
    def test_private_and_loopback_blocked(self):
        for url in ("http://127.0.0.1:8787/api/stats", "http://10.0.0.1/x",
                    "https://192.168.1.1/", "http://169.254.169.254/latest/meta-data",
                    "http://[::1]/", "http://0.0.0.0/"):
            self.assertFalse(summarize.url_is_public(url), url)

    def test_public_ip_literal_allowed(self):
        self.assertTrue(summarize.url_is_public("https://1.1.1.1/page"))

    def test_non_http_schemes_blocked(self):
        for url in ("ftp://1.1.1.1/x", "file:///etc/passwd", "", None, "not a url"):
            self.assertFalse(summarize.url_is_public(url), url)


# --------------------------------------------------------------------------- #
# prompt 构造 / 材料合成（纯函数）
# --------------------------------------------------------------------------- #
class TestBuildPrompt(unittest.TestCase):
    def test_contains_sid_title_and_material(self):
        p = summarize.build_prompt([
            {"sid": "aaaa000001", "title": "T0", "source_name": "Src", "category": "paper", "material": "M0"},
            {"sid": "bbbb000002", "title": "T1", "source_name": "Src", "category": None, "material": ""},
        ])
        self.assertIn("[aaaa000001] 标题: T0", p)
        self.assertIn("M0", p)
        self.assertIn("[bbbb000002] 标题: T1", p)
        self.assertIn("（无，只有标题）", p)   # 空材料如实标注
        self.assertIn("2 条", p)


class TestMaterialFor(unittest.TestCase):
    def test_content_plus_distinct_summary_are_combined(self):
        out = summarize.material_for({"summary": "short feed summary"}, "long fetched body")
        self.assertIn("short feed summary", out)
        self.assertIn("long fetched body", out)

    def test_summary_already_inside_content_not_duplicated(self):
        body = "intro… short feed summary …rest of page"
        out = summarize.material_for({"summary": "short feed summary"}, body)
        self.assertEqual(out, body)

    def test_falls_back_to_summary_then_empty(self):
        self.assertEqual(summarize.material_for({"summary": "only summary"}, ""), "only summary")
        self.assertEqual(summarize.material_for({"summary": ""}, ""), "")


# --------------------------------------------------------------------------- #
# LLM 回复解析（纯函数）
# --------------------------------------------------------------------------- #
class TestParseLlmReply(unittest.TestCase):
    SID = "aaaa000001"
    SID2 = "bbbb000002"
    GOOD = f'[{{"id":"{SID}","summary":"中文摘要","points":["a","b"],"why":"重要"}}]'

    def test_plain_array(self):
        out = summarize.parse_llm_reply(self.GOOD, {self.SID})
        self.assertEqual(out[self.SID]["summary"], "中文摘要")
        self.assertEqual(out[self.SID]["points"], ["a", "b"])
        self.assertEqual(out[self.SID]["why"], "重要")

    def test_markdown_fence_tolerated(self):
        out = summarize.parse_llm_reply(f"```json\n{self.GOOD}\n```", {self.SID})
        self.assertIn(self.SID, out)

    def test_prose_around_array_tolerated(self):
        out = summarize.parse_llm_reply(f"好的，结果如下：\n{self.GOOD}\n以上。", {self.SID})
        self.assertIn(self.SID, out)

    def test_unexpected_or_duplicate_id_dropped(self):
        text = (f'[{{"id":"ffff999999","summary":"不在预期"}},'
                f' {{"id":"{self.SID}","summary":"第一个"}},'
                f' {{"id":"{self.SID}","summary":"重复的"}}]')
        out = summarize.parse_llm_reply(text, {self.SID})
        self.assertEqual(set(out), {self.SID})
        self.assertEqual(out[self.SID]["summary"], "第一个")

    def test_missing_summary_entry_dropped_not_fatal(self):
        text = (f'[{{"id":"{self.SID}","points":["x"]}},'
                f'{{"id":"{self.SID2}","summary":"有摘要"}}]')
        out = summarize.parse_llm_reply(text, {self.SID, self.SID2})
        self.assertEqual(set(out), {self.SID2})

    def test_bad_points_degrade_to_empty(self):
        out = summarize.parse_llm_reply(
            f'[{{"id":"{self.SID}","summary":"s","points":"oops"}}]', {self.SID})
        self.assertEqual(out[self.SID]["points"], [])

    def test_invented_ids_raise_for_retry(self):
        # LLM 自己编了编号（如 1-based 整数）：一条都对不上必须 raise 触发
        # 重试/二分，绝不能静默全丢、更不能错位写入。
        text = '[{"id":"1","summary":"a"},{"id":"2","summary":"b"}]'
        with self.assertRaises(ValueError):
            summarize.parse_llm_reply(text, {self.SID, self.SID2})

    def test_structural_garbage_raises(self):
        for bad in ("", "   ", "no array here", "[not json", '{"a": 1}'):
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                summarize.parse_llm_reply(bad, {self.SID})


# --------------------------------------------------------------------------- #
# 批量总结：重试 + 二分降级（fake runner，零网络）
# --------------------------------------------------------------------------- #
def echo_runner_factory(poison_title=None, fail_first_n=0, log=None):
    """按 prompt 里的 `[sid] 标题:` 行回出合法 JSON 的 fake runner。

    poison_title: prompt 里含这个标题就回垃圾（模拟某条内容打崩 JSON）。
    fail_first_n: 前 n 次调用回垃圾（模拟 LLM 抖动，重试后恢复）。
    """
    calls = {"n": 0}

    def runner(prompt):
        calls["n"] += 1
        if log is not None:
            log.append(prompt)
        if calls["n"] <= fail_first_n:
            return "GARBAGE not json", 0.01
        if poison_title and poison_title in prompt:
            return "GARBAGE not json", 0.01
        sids = re.findall(r"^\[(\w+)\] 标题:", prompt, re.M)
        reply = json.dumps([
            {"id": s, "summary": f"摘要{s}", "points": [f"点{s}"], "why": f"因{s}"}
            for s in sids
        ], ensure_ascii=False)
        return reply, 0.01

    runner.calls = calls
    return runner


class TestSummarizeBatch(unittest.TestCase):
    BATCH = [
        {"sid": "sida000001", "title": "Alpha", "source_name": "s", "category": "x", "material": "m"},
        {"sid": "sidb000002", "title": "Beta", "source_name": "s", "category": "x", "material": "m"},
        {"sid": "sidc000003", "title": "Gamma", "source_name": "s", "category": "x", "material": "m"},
        {"sid": "sidd000004", "title": "Delta", "source_name": "s", "category": "x", "material": "m"},
    ]
    ALL_SIDS = {"sida000001", "sidb000002", "sidc000003", "sidd000004"}

    def test_happy_path_single_call(self):
        runner = echo_runner_factory()
        results, cost = summarize.summarize_batch(self.BATCH, runner)
        self.assertEqual(set(results), self.ALL_SIDS)
        self.assertEqual(runner.calls["n"], 1)
        self.assertAlmostEqual(cost, 0.01)

    def test_transient_failure_retried_once(self):
        runner = echo_runner_factory(fail_first_n=1)
        results, cost = summarize.summarize_batch(self.BATCH, runner)
        self.assertEqual(set(results), self.ALL_SIDS)
        self.assertEqual(runner.calls["n"], 2)
        self.assertAlmostEqual(cost, 0.02)

    def test_poisoned_item_bisected_out_others_survive(self):
        # Beta 持续打崩 JSON：整批失败 → 二分 → 单条仍失败 → 只丢 Beta。
        import contextlib, io
        runner = echo_runner_factory(poison_title="Beta")
        with contextlib.redirect_stdout(io.StringIO()):
            results, _ = summarize.summarize_batch(self.BATCH, runner)
        self.assertEqual(set(results), self.ALL_SIDS - {"sidb000002"})

    def test_single_item_failure_returns_empty(self):
        import contextlib, io
        runner = echo_runner_factory(poison_title="Alpha")
        with contextlib.redirect_stdout(io.StringIO()):
            results, _ = summarize.summarize_batch(self.BATCH[:1], runner)
        self.assertEqual(results, {})


class TestFuseRunner(unittest.TestCase):
    def test_systemic_failure_blows_fuse_and_stops_real_calls(self):
        # 系统性故障（未登录/配额耗尽）：没有熔断时一批最坏 4B-2 次真调用。
        # 熔断（连续 3 次）后，重试/二分仍走流程但每次秒失败、零 LLM 开销。
        import contextlib, io
        calls = {"n": 0}

        def always_fail(prompt):
            calls["n"] += 1
            raise RuntimeError("not logged in")

        fuse = summarize.FuseRunner(always_fail, max_consecutive=3)
        with contextlib.redirect_stdout(io.StringIO()):
            results, _ = summarize.summarize_batch(TestSummarizeBatch.BATCH, fuse)
        self.assertEqual(results, {})
        self.assertEqual(calls["n"], 3)   # 真调用止步于熔断阈值（无熔断是 4*4-2=14）
        self.assertTrue(fuse.blown)

    def test_success_resets_counter(self):
        seq = {"n": 0}

        def flaky(prompt):
            seq["n"] += 1
            if seq["n"] <= 1:
                raise RuntimeError("transient")
            return '[{"id":"sida000001","summary":"好了"}]', 0.01

        fuse = summarize.FuseRunner(flaky, max_consecutive=3)
        results, _ = summarize.summarize_batch(TestSummarizeBatch.BATCH[:1], fuse)
        self.assertIn("sida000001", results)
        self.assertFalse(fuse.blown)  # 成功清零，没熔断


# --------------------------------------------------------------------------- #
# 落库不变量：只写 ai_*，triage 字段一个都不许动
# --------------------------------------------------------------------------- #
class TestApplyResults(TmpDbTestCase):
    def test_writes_ai_fields_only_and_never_touches_triage(self):
        conn = self.fresh_conn()
        self.insert_item(conn, "a", title="T")
        conn.execute(
            "UPDATE items SET score=4, tags='keep,me', status='promoted',"
            " comment='user note', triaged_at='2026-06-01T00:00:00+00:00' WHERE id='a'"
        )
        conn.commit()

        batch = [{"sid": "sid-a", "id": "a"}]
        results = {"sid-a": {"summary": "一句话", "points": ["p1", "p2"], "why": "因为"}}
        n = summarize.apply_results(conn, batch, results, model="haiku")
        self.assertEqual(n, 1)

        row = conn.execute("SELECT * FROM items WHERE id='a'").fetchone()
        self.assertEqual(row["ai_summary"], "一句话")
        self.assertEqual(json.loads(row["ai_detail"]), {"points": ["p1", "p2"], "why": "因为"})
        self.assertEqual(row["ai_model"], "haiku")
        self.assertIsNotNone(row["ai_summarized_at"])
        # triage 不变量
        self.assertEqual(
            (row["score"], row["tags"], row["status"], row["comment"], row["triaged_at"]),
            (4, "keep,me", "promoted", "user note", "2026-06-01T00:00:00+00:00"),
        )

    def test_items_without_result_left_unsummarized(self):
        conn = self.fresh_conn()
        self.insert_item(conn, "a")
        self.insert_item(conn, "b", url="https://x/2")
        batch = [{"sid": "sid-a", "id": "a"}, {"sid": "sid-b", "id": "b"}]
        n = summarize.apply_results(conn, batch, {"sid-a": {"summary": "s", "points": [], "why": ""}}, "haiku")
        self.assertEqual(n, 1)
        b = conn.execute("SELECT ai_summary, ai_summarized_at FROM items WHERE id='b'").fetchone()
        self.assertIsNone(b["ai_summary"])
        self.assertIsNone(b["ai_summarized_at"])  # 下次还会被 pick_items 选中


# --------------------------------------------------------------------------- #
# fetch_contents / run()：dry-run 绝不写库（注入 fake fetch_content，零网络）
# --------------------------------------------------------------------------- #
class TestFetchContentsAndDryRun(TmpDbTestCase):
    def _patch_fetch(self, body="fetched body text"):
        orig = summarize.fetch_content
        summarize.fetch_content = lambda url: body
        self.addCleanup(lambda: setattr(summarize, "fetch_content", orig))

    def test_fetch_contents_skips_items_with_existing_text(self):
        self._patch_fetch()
        items = [
            {"id": "a", "url": "https://x/1", "content_text": None},
            {"id": "b", "url": "https://x/2", "content_text": "already here"},
        ]
        out = summarize.fetch_contents(items)
        self.assertEqual(out, {"a": "fetched body text"})

    def test_dry_run_writes_nothing_at_all(self):
        import contextlib, io
        conn = self.fresh_conn()
        self.insert_item(conn, "a", url="https://x/1")
        self.insert_item(conn, "b", url="https://x/2")
        self._patch_fetch()
        orig_db = summarize.DB_PATH
        summarize.DB_PATH = self.db_path
        self.addCleanup(lambda: setattr(summarize, "DB_PATH", orig_db))
        with contextlib.redirect_stdout(io.StringIO()):
            summarize.run(limit=10, source=None, model="haiku",
                          batch_size=8, no_fetch=False, dry_run=True)
        rows = conn.execute(
            "SELECT content_text, ai_summary, ai_summarized_at FROM items").fetchall()
        for r in rows:  # 抓到的正文也不许落库——dry-run 是零写入承诺
            self.assertIsNone(r["content_text"])
            self.assertIsNone(r["ai_summary"])
            self.assertIsNone(r["ai_summarized_at"])


class TestCliGuards(unittest.TestCase):
    def test_negative_limit_rejected(self):
        # SQLite 把负 LIMIT 当「无上限」，--limit -1 会静默等价 --all（花真钱）。
        import contextlib, io
        from unittest import mock
        with mock.patch.object(sys, "argv", ["summarize.py", "--limit", "-1"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    summarize.main()


if __name__ == "__main__":
    unittest.main()
