#!/usr/bin/env python3
"""mysqlite — 从零实现的迷你 SQLite。

M1 pager + REPL:定长页(4KB)分页文件、页缓存与落盘、行序列化
   (id + username + email)、单叶结点按 id 有序插入、全表扫描、REPL。
M2 B-tree:叶结点分裂、内部结点(cell = 子页 + 子树最大键)、
   递归插入与分裂上提、多级树、叶链(供 M3 范围扫描)、.dumptree
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


# ---------- 内部结点(M2) ----------
# 布局:type(1) + cell_count(2) + right_child(4) + cells
# 每个格子:child_page(4) + 该子树的最大键(4)

INTERNAL_CELL_SIZE = 8
INTERNAL_MAX_CELLS = (PAGE_SIZE - LEAF_HEADER_SIZE) // INTERNAL_CELL_SIZE  # 511


def internal_init(page):
    page[0] = NODE_INTERNAL
    struct.pack_into(">H", page, 1, 0)
    struct.pack_into(">I", page, 3, 0)


def internal_cell_count(page):
    return struct.unpack_from(">H", page, 1)[0]


def internal_set_cell_count(page, count):
    struct.pack_into(">H", page, 1, count)


def internal_right_child(page):
    return struct.unpack_from(">I", page, 3)[0]


def internal_set_right_child(page, n):
    struct.pack_into(">I", page, 3, n)


def internal_cell(page, i):
    """返回 (child_page, 子树最大键)。"""
    start = LEAF_HEADER_SIZE + i * INTERNAL_CELL_SIZE
    child, key = struct.unpack_from(">II", page, start)
    return child, key


def internal_write_cell(page, i, child, key):
    start = LEAF_HEADER_SIZE + i * INTERNAL_CELL_SIZE
    struct.pack_into(">II", page, start, child, key)


# ---------- 表:插入与全表扫描 ----------

class Table:
    def __init__(self, pager):
        self.pager = pager

    # ----- 插入:递归下降 + 分裂上提 -----

    def insert(self, row):
        result = self._insert(self.pager.root_page, row)
        if result is not None:
            # 根分裂:建一个新的内部结点当根
            left_max, _right_max, new_right = result
            old_root = self.pager.root_page
            root_no, root = self.pager.new_page()
            internal_init(root)
            internal_write_cell(root, 0, old_root, left_max)
            internal_set_right_child(root, new_right)
            internal_set_cell_count(root, 1)
            self.pager.root_page = root_no

    def _insert(self, page_no, row):
        """递归插入。子结点分裂时返回 (左子树max, 右子树max, 新右结点页号)。"""
        page = self.pager.get_page(page_no)
        if page[0] == NODE_LEAF:
            return self._insert_leaf(page_no, page, row)
        count = internal_cell_count(page)
        child = internal_right_child(page)
        cell_index = count  # 默认下降进右孩子
        for i in range(count):
            c, key = internal_cell(page, i)
            if key >= row.id:
                child, cell_index = c, i
                break
        result = self._insert(child, row)
        if result is None:
            return None
        left_max, right_max, new_right = result
        # 先在逻辑层面组装格子,页内永远不出现第 MAX+1 个格子
        cells = [internal_cell(page, i) for i in range(count)]
        if cell_index == count:
            # 右孩子分裂:左半保留为右孩子,原右孩子降格成格子,新结点接管右孩子
            cells.append((child, left_max))
            new_right_child = new_right
        else:
            # 下降进的那个格子分裂:更新它的键,新结点插到它后面
            old_child, _old_key = cells[cell_index]
            cells[cell_index] = (old_child, left_max)
            cells.insert(cell_index + 1, (new_right, right_max))
            new_right_child = internal_right_child(page)
        if len(cells) <= INTERNAL_MAX_CELLS:
            internal_set_cell_count(page, len(cells))
            for i, (c, k) in enumerate(cells):
                internal_write_cell(page, i, c, k)
            internal_set_right_child(page, new_right_child)
            return None
        return self._split_internal_cells(page, cells, new_right_child)

    def _insert_leaf(self, _page_no, page, row):
        data = serialize_row(row)
        count = leaf_cell_count(page)
        ids = [leaf_cell_id(page, i) for i in range(count)]
        pos = bisect.bisect_left(ids, row.id)
        if pos < count and ids[pos] == row.id:
            raise MysqliteError(f"id {row.id} 已存在")
        if count < LEAF_MAX_CELLS:
            for i in range(count, pos, -1):
                leaf_write_cell(page, i, leaf_cell(page, i - 1))
            leaf_write_cell(page, pos, data)
            leaf_set_cell_count(page, count + 1)
            return None
        # 满:插入逻辑序列后对半分裂
        cells = [leaf_cell(page, i) for i in range(count)]
        cells.insert(pos, data)
        split_at = len(cells) // 2  # 左半行数
        leaf_set_cell_count(page, split_at)
        for i in range(split_at):
            leaf_write_cell(page, i, cells[i])
        new_no, new_page = self.pager.new_page()
        leaf_init(new_page)
        right_cells = cells[split_at:]
        for i, cell in enumerate(right_cells):
            leaf_write_cell(new_page, i, cell)
        leaf_set_cell_count(new_page, len(right_cells))
        # 维护叶链(M3 范围扫描用)
        leaf_set_next(new_page, leaf_next(page))
        leaf_set_next(page, new_no)
        return (leaf_cell_id(page, split_at - 1),
                leaf_cell_id(new_page, len(right_cells) - 1),
                new_no)

    def _split_internal_cells(self, page, cells, right_child):
        """把"已超容的格子列表 + 右孩子"对半分裂成两个内部结点。"""
        count = len(cells)
        split_at = count // 2
        split_child, split_key = cells[split_at]
        # 左半:cells[:split_at],右孩子指向 cells[split_at] 的孩子;
        # 左半子树最大键恰好就是被抬上去的分隔键 split_key
        internal_set_cell_count(page, split_at)
        for i in range(split_at):
            c, k = cells[i]
            internal_write_cell(page, i, c, k)
        internal_set_right_child(page, split_child)
        # 右半:cells[split_at+1:],右孩子为原右孩子
        new_no, new_page = self.pager.new_page()
        internal_init(new_page)
        right_cells = cells[split_at + 1:]
        for i, (c, k) in enumerate(right_cells):
            internal_write_cell(new_page, i, c, k)
        internal_set_cell_count(new_page, len(right_cells))
        internal_set_right_child(new_page, right_child)
        return (split_key, self._subtree_max(right_child), new_no)

    def _subtree_max(self, page_no):
        page = self.pager.get_page(page_no)
        while page[0] == NODE_INTERNAL:
            page_no = internal_right_child(page)
            page = self.pager.get_page(page_no)
        return leaf_cell_id(page, leaf_cell_count(page) - 1)

    # ----- 查询 -----

    def scan(self):
        """中序遍历整棵树,按 id 有序产出全部行。"""
        yield from self._scan(self.pager.root_page)

    def _scan(self, page_no):
        page = self.pager.get_page(page_no)
        if page[0] == NODE_LEAF:
            for i in range(leaf_cell_count(page)):
                yield deserialize_row(leaf_cell(page, i))
            return
        count = internal_cell_count(page)
        for i in range(count):
            yield from self._scan(internal_cell(page, i)[0])
        yield from self._scan(internal_right_child(page))

    def dump_tree(self):
        """返回 (树形文本, 深度层数),供 .dumptree 与测试使用。"""
        lines = []
        deepest = [0]

        def walk(page_no, depth):
            page = self.pager.get_page(page_no)
            indent = "  " * depth
            deepest[0] = max(deepest[0], depth + 1)
            if page[0] == NODE_LEAF:
                count = leaf_cell_count(page)
                if count:
                    text = (f"页 {page_no} [叶] {count} 行 "
                            f"(id {leaf_cell_id(page, 0)}..{leaf_cell_id(page, count - 1)})")
                else:
                    text = f"页 {page_no} [叶] 空"
                lines.append(indent + text)
                return
            count = internal_cell_count(page)
            keys = [internal_cell(page, i)[1] for i in range(count)]
            lines.append(f"{indent}页 {page_no} [内部] {count} 格子 "
                         f"键 {keys} 右孩子 页 {internal_right_child(page)}")
            for i in range(count):
                walk(internal_cell(page, i)[0], depth + 1)
            walk(internal_right_child(page), depth + 1)

        walk(self.pager.root_page, 0)
        lines.append(f"深度: {deepest[0]} 层  总页数: {self.pager.num_pages}")
        return "\n".join(lines), deepest[0]

    def close(self):
        self.pager.flush()


# ---------- REPL 与命令门面 ----------

HELP = """\
命令:
  insert <id> <username> <email>   插入一行
  select                           全表扫描(按 id 有序)
元命令:
  .dbinfo    显示数据库信息
  .dumptree  打印 B-tree 结构(M2)
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
        if line == ".dumptree":
            text, _depth = self.table.dump_tree()
            return text
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
