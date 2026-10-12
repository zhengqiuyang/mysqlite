# mysqlite — 从零实现的迷你 SQLite

[![CI](https://github.com/zhengqiuyang/mysqlite/actions/workflows/ci.yml/badge.svg)](https://github.com/zhengqiuyang/mysqlite/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-informational.svg)](./LICENSE)

用纯 Python 从存储引擎的层级往上实现一个迷你数据库:分页文件 → B-tree →
带 WHERE 的查询,配上一个类 SQLite 的 REPL。单文件、零依赖。

## 快速开始

```bash
python mysqlite.py mydb.db
mysqlite> insert 1 alice alice@example.com
Executed.
mysqlite> insert 2 bob bob@example.com
Executed.
mysqlite> select
(1, alice, alice@example.com)
(2, bob, bob@example.com)
mysqlite> .exit
```

## 文件格式

- 定长页 4KB;页 0 是数据库头(magic `MYDB` + 页大小 + 根页号 + 总页数),树结点占页 1..N。
- 行布局:`id`(4 字节大端)+ `username`(32 字节 UTF-8)+ `email`(255 字节),定长 291 字节。
- 叶结点:`type(1) + cell_count(2) + next_leaf(4) + cells`,每页最多 14 行,按 id 有序。
- 内部结点(M2):`type(1) + cell_count(2) + right_child(4) + cells`,
  每个格子是 `child_page(4) + 子树最大键(4)`。

## 里程碑

- [x] **M1 pager + REPL**:分页文件的读/缓存/分配/落盘、行序列化(字节级长度校验)、
  单叶结点按 id 有序插入(二分定位 + 右移)、全表扫描、REPL(insert/select/.dbinfo/.help)、
  关库落盘与重开恢复(14 个单测,含子进程集成)。
- [ ] M2 B-tree:叶结点分裂、内部结点、多级树、`.dumptree`
- [ ] M3 WHERE:id 等值走树下降查找,范围走叶链,其他列全表扫描过滤
