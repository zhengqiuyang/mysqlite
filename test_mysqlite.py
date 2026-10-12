"""mysqlite M1 的单元测试:python test_mysqlite.py -v"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mysqlite import (  # noqa: E402
    LEAF_HEADER_SIZE, LEAF_MAX_CELLS, MysqliteError, Pager, Row, Table,
    deserialize_row, serialize_row,
)

HERE = os.path.dirname(os.path.abspath(__file__))


class RowSerializationTests(unittest.TestCase):
    def test_roundtrip(self):
        row = Row(42, "alice", "alice@example.com")
        self.assertEqual(deserialize_row(serialize_row(row)), row)

    def test_chinese_username_fits_by_bytes(self):
        # 32 字节上限按 UTF-8 字节数算,不是字符数
        row = Row(7, "郑润泽", "ry@local")
        self.assertEqual(deserialize_row(serialize_row(row)), row)

    def test_too_long_rejected(self):
        with self.assertRaises(MysqliteError) as ctx:
            serialize_row(Row(1, "x" * 33, "a@b"))
        self.assertIn("username 过长", str(ctx.exception))
        with self.assertRaises(MysqliteError):
            serialize_row(Row(1, "ok", "e" * 256))

    def test_row_size_fits_leaf(self):
        data = serialize_row(Row(1, "u", "e"))
        self.assertEqual(len(data), 291)
        self.assertLessEqual(LEAF_HEADER_SIZE + LEAF_MAX_CELLS * len(data), 4096)


class TableTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "test.db")
        self.table = Table(Pager(self.path))

    def close_and_reopen(self):
        self.table.close()
        self.table = Table(Pager(self.path))
        return self.table

    def test_insert_and_scan_sorted(self):
        for row_id in [5, 1, 3, 2, 4]:
            self.table.insert(Row(row_id, f"user{row_id}", f"u{row_id}@x"))
        ids = [row.id for row in self.table.scan()]
        self.assertEqual(ids, [1, 2, 3, 4, 5])

    def test_duplicate_id_rejected(self):
        self.table.insert(Row(1, "a", "a@x"))
        with self.assertRaises(MysqliteError) as ctx:
            self.table.insert(Row(1, "b", "b@x"))
        self.assertIn("已存在", str(ctx.exception))

    def test_grows_beyond_single_leaf(self):
        # M2 起叶满会分裂,不再报"表已满"
        total = LEAF_MAX_CELLS + 10
        for i in range(total):
            self.table.insert(Row(i, f"u{i}", "e@x"))
        self.assertEqual([r.id for r in self.table.scan()], list(range(total)))

    def test_persistence_across_close(self):
        for i in [10, 3, 7]:
            self.table.insert(Row(i, f"u{i}", f"u{i}@x"))
        self.close_and_reopen()
        rows = list(self.table.scan())
        self.assertEqual([r.id for r in rows], [3, 7, 10])
        self.assertEqual(rows[0].username, "u3")

    def test_reopen_bad_file_rejected(self):
        self.table.close()
        with open(self.path, "wb") as f:
            f.write(b"not a mysqlite file at all")
        with self.assertRaises(MysqliteError):
            Pager(self.path)

    def test_insert_after_reopen_appends(self):
        self.table.insert(Row(1, "a", "a@x"))
        self.close_and_reopen()
        self.table.insert(Row(2, "b", "b@x"))
        self.assertEqual([r.id for r in self.table.scan()], [1, 2])


class BTreeTests(unittest.TestCase):
    """M2:叶分裂、内部结点、多级树。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "btree.db")
        self.table = Table(Pager(self.path))

    def ids(self):
        return [r.id for r in self.table.scan()]

    def test_first_split_creates_internal_root(self):
        for i in range(LEAF_MAX_CELLS + 1):  # 15 行触发第一次分裂
            self.table.insert(Row(i, f"u{i}", "e@x"))
        root = self.table.pager.get_page(self.table.pager.root_page)
        self.assertEqual(root[0], 2)  # NODE_INTERNAL
        self.assertEqual(self.ids(), list(range(LEAF_MAX_CELLS + 1)))

    def test_split_with_descending_inserts(self):
        # 逆序插入最容易暴露分裂/搬运 bug
        for i in reversed(range(50)):
            self.table.insert(Row(i, f"u{i}", "e@x"))
        self.assertEqual(self.ids(), list(range(50)))

    def test_many_shuffled_rows(self):
        import random
        ids = list(range(200))
        random.Random(42).shuffle(ids)
        for i in ids:
            self.table.insert(Row(i, f"user{i}", f"u{i}@x"))
        self.assertEqual(self.ids(), list(range(200)))
        # 抽查内容没有被搬错
        rows = {r.id: r for r in self.table.scan()}
        self.assertEqual(rows[137].username, "user137")

    def test_deep_tree_three_levels(self):
        # 每叶 14 行;内部结点最多 511 格 → 511*14=7154 行内是两层,
        # 超过 7154 行必然出现三层
        total = 8000
        for i in range(total):
            self.table.insert(Row(i, f"u{i}", "e@x"))
        text, depth = self.table.dump_tree()
        self.assertGreaterEqual(depth, 3)
        self.assertEqual(len(self.ids()), total)
        # 根 + 至少 2 个中间层内部结点
        self.assertGreaterEqual(text.count("[内部]"), 3)

    def test_deep_tree_persistence(self):
        total = 3000
        for i in range(total):
            self.table.insert(Row(i, f"u{i}", "e@x"))
        self.table.close()
        self.table = Table(Pager(self.path))
        self.assertEqual(len(self.ids()), total)
        rows = {r.id: r for r in self.table.scan()}
        self.assertEqual(rows[2999].username, "u2999")

    def test_duplicate_id_in_tree(self):
        for i in range(30):
            self.table.insert(Row(i, "u", "e@x"))
        with self.assertRaises(MysqliteError) as ctx:
            self.table.insert(Row(17, "dup", "d@x"))
        self.assertIn("已存在", str(ctx.exception))

    def test_dumptree_output(self):
        for i in range(30):
            self.table.insert(Row(i, f"u{i}", "e@x"))
        text, depth = self.table.dump_tree()
        self.assertIn("[内部]", text)
        self.assertIn("[叶]", text)
        self.assertIn("深度:", text)
        self.assertGreaterEqual(depth, 2)


class ReplTests(unittest.TestCase):
    def run_repl(self, script, db_path):
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "mysqlite.py"), db_path],
            input=script, capture_output=True, text=True, timeout=60)

    def test_full_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "s.db")
            result = self.run_repl(
                "insert 1 alice alice@x.com\n"
                "insert 2 bob bob@x.com\n"
                "select\n"
                ".dbinfo\n"
                ".exit\n", db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(1, alice, alice@x.com)", result.stdout)
        self.assertIn("(2, bob, bob@x.com)", result.stdout)
        self.assertIn("行数: 2", result.stdout)

    def test_repl_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "p.db")
            result = self.run_repl("insert 5 carol c@x.com\n.exit\n", db)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.run_repl("select\n.exit\n", db)
        self.assertIn("(5, carol, c@x.com)", result.stdout)

    def test_repl_errors_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_repl(
                "bogus command\n"
                "insert 1\n"
                "insert x a b\n"
                "insert 1 a a@x\n"
                "insert 1 a a@x\n"   # 重复 id
                "select\n"
                ".exit\n", os.path.join(tmp, "e.db"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("无法识别的命令", result.stdout)
        self.assertIn("用法", result.stdout)
        self.assertIn("id 必须是整数", result.stdout)
        self.assertIn("已存在", result.stdout)
        self.assertIn("(1, a, a@x)", result.stdout)

    def test_empty_select_and_help(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_repl(
                "select\n.help\n.exit\n", os.path.join(tmp, "h.db"))
        self.assertIn("(空表)", result.stdout)
        self.assertIn("insert <id> <username> <email>", result.stdout)


if __name__ == "__main__":
    unittest.main()
