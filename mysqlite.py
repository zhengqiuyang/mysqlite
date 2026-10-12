#!/usr/bin/env python3
"""mysqlite — 从零实现的迷你 SQLite。

M1 pager + REPL:定长页(4KB)分页文件、页缓存与落盘、行序列化
   (id + username + email)、单叶结点按 id 有序插入、全表扫描、REPL。
M2 B-tree:叶结点分裂、内部结点、多级树
M3 WHERE:id 等值走树下降查找,范围走叶链,其他列全表扫描过滤

文件布局:页 0 是数据库头(magic + 页大小 + 根页号 + 总页数),
树结点占页 1..N。页内格式见各结点函数的注释。
"""
import bisect
import os
import struct
import sys

PAGE_SIZE = 4096
MAGIC = b"MYDB"
HEADER_SIZE = 16  # magic(4) + page_size(4) + root_page(4) + num_pages(4)

NODE_LEAF = 1
NODE_INTERNAL = 2

# 行布局:id(4 字节大端) + username(32 字节) + email(255 字节)
USERNAME_SIZE = 32
EMAIL_SIZE = 255
ROW_SIZE = 4 + USERNAME_SIZE + EMAIL_SIZE  # 291

# 叶结点:type(1) + cell_count(2) + next_leaf(4) + cells
LEAF_HEADER_SIZE = 7
LEAF_MAX_CELLS = (PAGE_SIZE - LEAF_HEADER_SIZE) // ROW_SIZE  # 14


class MysqliteError(Exception):
    """mysqlite 的所有用户可见错误。"""


class Row:
    __slots__ = ("id", "username", "email")

    def __init__(self, row_id, username, email):
        self.id = row_id
        self.username = username
        self.email = email

    def __repr__(self):
        return f"({self.id}, {self.username}, {self.email})"

    def __eq__(self, other):
        if not isinstance(other, Row):
            return NotImplemented
        return (self.id, self.username, self.email) == (
            other.id, other.username, other.email)


def serialize_row(row):
    username = row.username.encode("utf-8")
    email = row.email.encode("utf-8")
    if len(username) > USERNAME_SIZE:
        raise MysqliteError(f"username 过长({len(username)} 字节,上限 {USERNAME_SIZE})")
    if len(email) > EMAIL_SIZE:
        raise MysqliteError(f"email 过长({len(email)} 字节,上限 {EMAIL_SIZE})")
    return (struct.pack(">I", row.id)
            + username.ljust(USERNAME_SIZE, b"\0")
            + email.ljust(EMAIL_SIZE, b"\0"))


def deserialize_row(data):
    (row_id,) = struct.unpack_from(">I", data, 0)
    username = data[4:4 + USERNAME_SIZE].rstrip(b"\0").decode("utf-8")
    email = data[4 + USERNAME_SIZE:4 + USERNAME_SIZE + EMAIL_SIZE]
    return Row(row_id, username, email.rstrip(b"\0").decode("utf-8"))


# ---------- pager:分页文件管理 ----------

class Pager:
    """定长页的读、缓存、分配与落盘。页 0 是数据库头,树结点从页 1 开始。"""

    def __init__(self, path):
        self.path = path
        self.pages = {}  # 页号 -> bytearray
        self.num_pages = 0
        self.root_page = 0
        self._load_or_init()

    def _load_or_init(self):
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            with open(self.path, "rb") as f:
                header = f.read(HEADER_SIZE)
            if len(header) < HEADER_SIZE or header[:4] != MAGIC:
                raise MysqliteError(f"{self.path} 不是 mysqlite 数据库文件")
            _magic, page_size, self.root_page, self.num_pages = struct.unpack(
                ">4sIII", header)
            if page_size != PAGE_SIZE:
                raise MysqliteError(f"不支持的页大小 {page_size}(期望 {PAGE_SIZE})")
        else:
            self.num_pages = 1  # 只有页 0(数据库头)
            self.root_page = self.new_page()[0]
            leaf_init(self.get_page(self.root_page))

    @staticmethod
    def page_offset(n):
        return n * PAGE_SIZE

    def get_page(self, n):
        if n < 1 or n >= self.num_pages:
            raise MysqliteError(f"页 {n} 不存在(共 {self.num_pages} 页)")
        if n not in self.pages:
            with open(self.path, "rb") as f:
                f.seek(self.page_offset(n))
                data = f.read(PAGE_SIZE)
            if len(data) < PAGE_SIZE:
                data = data.ljust(PAGE_SIZE, b"\0")
            self.pages[n] = bytearray(data)
        return self.pages[n]

    def new_page(self):
        """分配一个新页,返回 (页号, bytearray)。"""
        n = self.num_pages
        self.num_pages += 1
        page = bytearray(PAGE_SIZE)
        self.pages[n] = page
        return n, page

    def flush(self):
        mode = "r+b" if os.path.exists(self.path) else "wb"
        with open(self.path, mode) as f:
            f.seek(0)
            f.write(struct.pack(">4sIII", MAGIC, PAGE_SIZE,
                                self.root_page, self.num_pages))
            for n in sorted(self.pages):
                if n == 0:
                    continue  # 页 0 只使用头部 16 字节
                f.seek(self.page_offset(n))
                f.write(bytes(self.pages[n]))


# ---------- 叶结点 ----------

def leaf_init(page):
    page[0] = NODE_LEAF
    struct.pack_into(">H", page, 1, 0)   # cell_count
    struct.pack_into(">I", page, 3, 0)   # next_leaf(0 = 无)


def leaf_cell_count(page):
    return struct.unpack_from(">H", page, 1)[0]


def leaf_set_cell_count(page, count):
    struct.pack_into(">H", page, 1, count)


def leaf_next(page):
    return struct.unpack_from(">I", page, 3)[0]


def leaf_set_next(page, n):
    struct.pack_into(">I", page, 3, n)


def leaf_cell(page, i):
    start = LEAF_HEADER_SIZE + i * ROW_SIZE
    return bytes(page[start:start + ROW_SIZE])


def leaf_write_cell(page, i, data):
    start = LEAF_HEADER_SIZE + i * ROW_SIZE
    page[start:start + ROW_SIZE] = data


def leaf_cell_id(page, i):
    return struct.unpack_from(">I", page, LEAF_HEADER_SIZE + i * ROW_SIZE)[0]


# ---------- 表:插入与全表扫描 ----------

class Table:
    def __init__(self, pager):
        self.pager = pager

    def insert(self, row):
        data = serialize_row(row)
        page = self.pager.get_page(self.pager.root_page)
        if page[0] != NODE_LEAF:
            raise MysqliteError("根结点不是叶结点(M2 引入内部结点后才会出现)")
        count = leaf_cell_count(page)
        if count >= LEAF_MAX_CELLS:
            raise MysqliteError(
                f"表已满:单叶结点最多 {LEAF_MAX_CELLS} 行"
                "(结点分裂在 M2 实现)")
        ids = [leaf_cell_id(page, i) for i in range(count)]
        pos = bisect.bisect_left(ids, row.id)
        if pos < count and ids[pos] == row.id:
            raise MysqliteError(f"id {row.id} 已存在")
        for i in range(count, pos, -1):  # 右移腾位
            leaf_write_cell(page, i, leaf_cell(page, i - 1))
        leaf_write_cell(page, pos, data)
        leaf_set_cell_count(page, count + 1)

    def scan(self):
        page = self.pager.get_page(self.pager.root_page)
        for i in range(leaf_cell_count(page)):
            yield deserialize_row(leaf_cell(page, i))

    def close(self):
        self.pager.flush()


# ---------- REPL 与命令门面 ----------

HELP = """\
命令:
  insert <id> <username> <email>   插入一行
  select                           全表扫描(按 id 有序)
元命令:
  .dbinfo    显示数据库信息
  .help      本帮助
  .exit      退出并落盘"""

EXIT = object()  # .exit 的哨兵


class Db:
    """命令解析与执行的门面,便于测试直接调用。"""

    def __init__(self, path):
        self.table = Table(Pager(path))

    def close(self):
        self.table.close()

    def execute(self, line):
        """执行一行命令,返回要打印的文本(None 表示无输出)。"""
        line = line.strip()
        if not line:
            return None
        if line.startswith("."):
            return self._dot_command(line)
        parts = line.split()
        cmd = parts[0].lower()
        if cmd == "insert":
            return self._insert(parts[1:])
        if cmd == "select":
            if len(parts) != 1:
                raise MysqliteError("M1 的 select 不带参数(WHERE 在 M3 实现)")
            rows = list(self.table.scan())
            if not rows:
                return "(空表)"
            return "\n".join(repr(row) for row in rows)
        raise MysqliteError(f"无法识别的命令 {cmd!r}(输入 .help 查看帮助)")

    def _insert(self, args):
        if len(args) != 3:
            raise MysqliteError("用法: insert <id> <username> <email>")
        try:
            row_id = int(args[0])
        except ValueError:
            raise MysqliteError("id 必须是整数") from None
        if not 0 <= row_id <= 0xFFFFFFFF:
            raise MysqliteError("id 超出范围(0 ~ 4294967295)")
        self.table.insert(Row(row_id, args[1], args[2]))
        return "Executed."

    def _dot_command(self, line):
        if line in (".exit", ".quit"):
            return EXIT
        if line == ".help":
            return HELP
        if line == ".dbinfo":
            rows = sum(1 for _ in self.table.scan())
            return (f"页大小: {PAGE_SIZE}  总页数: {self.table.pager.num_pages}  "
                    f"根页: {self.table.pager.root_page}  行数: {rows}  "
                    f"每叶最大行数: {LEAF_MAX_CELLS}")
        raise MysqliteError(f"未知的元命令 {line!r}(输入 .help 查看帮助)")


def repl(path):
    db = Db(path)
    print(f"mysqlite — 数据库文件: {path}(.help 查看帮助,.exit 退出)")
    while True:
        try:
            line = input("mysqlite> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            result = db.execute(line)
        except MysqliteError as e:
            print(f"错误: {e}")
            continue
        if result is EXIT:
            break
        if result is not None:
            print(result)
    db.close()
    print("Bye.")


def main(argv):
    if len(argv) > 2:
        print("用法: python mysqlite.py [dbfile]", file=sys.stderr)
        return 2
    repl(argv[1] if len(argv) == 2 else "mysqlite.db")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
