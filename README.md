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
- [x] **M2 B-tree**:叶结点对半分裂(页内永远先在逻辑层组装,不会写出第 15 格)、
  内部结点(格子 = 子页 + 子树最大键)、分裂递归上提、根分裂建新根、
  多级树(8000 行实测 3 层)、叶链维护、`.dumptree` 树形调试输出、
  中序遍历全表扫描(21 个单测)。
- [x] **M3 WHERE**:`select <列> <op> <值>`(op ∈ `=` `>` `<`)。
  id 等值走**树下降 + 叶内二分**(主键索引),id 范围沿**叶链**遍历
  (`<` 提前终止),username/email 全表扫描按字典序过滤(31 个单测)。
