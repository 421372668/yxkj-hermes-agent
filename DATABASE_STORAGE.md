# Hermes Agent 数据库存储配置指南

## 概述

Hermes Agent 支持通过配置切换数据库提供商，当前支持 **SQLite**（默认）和 **PostgreSQL**。

数据库抽象层位于 `hermes_db/` 包下，所有核心存储模块（`SessionDB`、`KanbanDB`、`ResponseStore`）均通过统一的 `DatabaseBackend` 接口访问数据库，切换提供商无需修改代码。

---

## 一、切换为 PostgreSQL

### 1. 安装依赖

PostgreSQL 后端依赖 `psycopg` 包：

```bash
# 推荐
pip install psycopg[binary]>=3.1

# 或使用项目可选依赖
pip install -e ".[postgres]"
```

> **注意**：`psycopg[binary]` 包含预编译的二进制文件，无需本地安装 PostgreSQL 客户端库。

### 2. 配置连接信息

#### 方式 A：通过 config.yaml（推荐）

在 `~/.hermes/config.yaml` 中添加：

```yaml
database:
  provider: postgresql
  postgresql:
    host: localhost
    port: 5432
    database: hermes
    user: hermes
    password: your_password
    # 连接池配置（可选）
    pool_min_size: 2
    pool_max_size: 10
```

也可以使用完整的连接字符串：

```yaml
database:
  provider: postgresql
  postgresql:
    connection_string: postgresql://hermes:your_password@localhost:5432/hermes
```

#### 方式 B：通过环境变量

设置以下环境变量即可切换，优先级高于 `config.yaml`：

```bash
# Windows PowerShell
$env:HERMES_DB_PROVIDER = "postgresql"
$env:HERMES_PG_CONNECTION_STRING = "postgresql://hermes:your_password@localhost:5432/hermes"

# Linux / macOS / WSL
export HERMES_DB_PROVIDER=postgresql
export HERMES_PG_CONNECTION_STRING="postgresql://hermes:your_password@localhost:5432/hermes"
```

### 3. 准备 PostgreSQL 数据库

在 PostgreSQL 中创建数据库（如果尚不存在）：

```sql
CREATE DATABASE hermes;
```

无需手动创建表，Hermes Agent 启动时会自动执行建表语句。

### 4. 数据迁移（从 SQLite 迁移到 PostgreSQL）

> **建议**：切换前先备份 SQLite 数据文件。

SQLite 数据文件默认位置（`~/.hermes/` 下）：

| 文件 | 用途 |
|------|------|
| `state.db` | 会话状态存储（SessionDB） |
| `kanban.db` | 看板任务存储（KanbanDB） |
| `response_store.db` | Responses API 存储（ResponseStore） |

Hermes Agent **不提供内置的数据迁移工具**。如果需要保留历史数据，建议使用第三方工具：

**使用 pgloader（推荐）**：

```bash
# 安装 pgloader
# macOS: brew install pgloader
# Linux: apt install pgloader

# 迁移 state.db
pgloader sqlite:///path/to/state.db postgresql://hermes:password@localhost:5432/hermes

# 迁移 kanban.db
pgloader sqlite:///path/to/kanban.db postgresql://hermes:password@localhost:5432/hermes
```

**手动迁移**：

如果数据量较小，可以直接通过 SQL 导出导入：

```bash
# 导出 SQLite 数据
sqlite3 ~/.hermes/state.db .dump > state_dump.sql

# 导入到 PostgreSQL（可能需要手动调整 SQL 语法）
psql -U hermes -d hermes -f state_dump.sql
```

> **注意**：SQLite FTS5 全文搜索表（`messages_fts`、`messages_fts_trigram`）在 PostgreSQL 中使用 `tsvector` + `pg_trgm` 替代，迁移时这些虚拟表会被自动跳过。

---

## 二、切换回 SQLite

### 通过 config.yaml

```yaml
database:
  provider: sqlite
```

或删除 `database.provider` 配置行（默认即为 sqlite）。

### 通过环境变量

```bash
# Windows PowerShell
$env:HERMES_DB_PROVIDER = "sqlite"

# Linux / macOS / WSL
export HERMES_DB_PROVIDER=sqlite
```

SQLite 数据文件默认路径：

| 数据库 | 默认路径 |
|--------|----------|
| state.db | `~/.hermes/state.db` |
| kanban.db | `~/.hermes/kanban.db` |
| response_store.db | `~/.hermes/response_store.db` |

可通过 `config.yaml` 自定义路径：

```yaml
database:
  provider: sqlite
  sqlite:
    state_db: /custom/path/state.db
    kanban_db: /custom/path/kanban.db
    response_store: /custom/path/response_store.db
```

---

## 三、配置项参考

### config.yaml 完整配置示例

```yaml
database:
  # 数据库提供商：sqlite（默认）| postgresql
  provider: sqlite

  # SQLite 配置（provider 为 sqlite 时生效）
  sqlite:
    state_db: ""          # 留空使用默认路径
    kanban_db: ""         # 留空使用默认路径
    response_store: ""    # 留空使用默认路径

  # PostgreSQL 配置（provider 为 postgresql 时生效）
  postgresql:
    host: localhost
    port: 5432
    database: hermes
    user: hermes
    password: ""
    # 连接字符串优先级高于单个字段
    connection_string: ""
    pool_min_size: 2
    pool_max_size: 10
```

### 环境变量参考

| 环境变量 | 说明 | 示例 |
|----------|------|------|
| `HERMES_DB_PROVIDER` | 数据库提供商 | `sqlite`, `postgresql` |
| `HERMES_PG_CONNECTION_STRING` | PostgreSQL 连接字符串 | `postgresql://user:pass@host:5432/db` |
| `HERMES_STATE_DB` | SQLite state.db 路径 | `/path/to/state.db` |
| `HERMES_KANBAN_DB` | SQLite kanban.db 路径 | `/path/to/kanban.db` |

---

## 四、验证切换是否成功

启动 Hermes Agent 后，可以通过以下方式验证当前使用的数据库：

```bash
# 查看日志输出
hermes --debug

# 日志中会显示类似信息：
# [hermes_db.config] Using database provider: postgresql
```

或在 Python 中验证：

```python
from hermes_db import get_db_config

config = get_db_config()
print(f"Current provider: {config.provider}")
# 输出: Current provider: postgresql
```

---

## 五、已知限制

1. **全文搜索差异**：PostgreSQL 使用 `tsvector` + `pg_trgm` 替代 SQLite FTS5，CJK 搜索行为可能略有差异
2. **WAL 检查点**：`PRAGMA wal_checkpoint` 是 SQLite 特有功能，PostgreSQL 模式自动跳过
3. **并发模型**：`BEGIN IMMEDIATE` 在 PostgreSQL 中等效于 `BEGIN` 并依赖 `Serializable` 隔离级别
4. **不支持的 SQLite 特性**：涉及 `sqlite_master` 表查询的代码仅在 SQLite 模式下工作
