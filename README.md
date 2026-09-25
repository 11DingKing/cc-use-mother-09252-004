# 数字教学资源联合发布

本项目用于建设面向业务人员的纯服务端系统。代码按领域模型、应用服务、持久化与接口边界组织；时间、标识和外部输入应通过可替换端口接入，以便稳定复现状态变化。运行数据与本地配置不得写入源码目录。

多国教师共同制作课程时，字幕、案例、版权范围和适用标准经常不同步。本服务在不覆盖各方原稿的前提下，管理资源组件、语言变体、版权授权、审核意见与依赖关系，仅当组合满足目标地区全部约束时才签发可发布版本；授权撤回后阻断未来发布并标出已发布版本的风险，历史一律保留。

## 架构

```
service_09252_004/
  fingerprint.py  内容指纹（规范化 JSON + SHA-256）：幂等写入与去重
  ports.py        可替换端口：时钟 / ID 生成器（测试注入 ManualClock、SequentialIds）
  errors.py       领域错误 → HTTP 状态码映射（400/404/409/422）
  storage.py      SQLite 持久化：单连接 + 可重入锁 + BEGIN IMMEDIATE 事务，WAL
  service.py      应用服务：组件/变体/授权/评审/依赖/候选/签发/回滚
  api.py          接口边界：标准库 HTTP JSON API（无第三方依赖）
  __main__.py     服务入口
```

关键语义：

- **不覆盖原稿**：组件与变体上传后不可变，按内容指纹去重；重复上传返回既有记录。
- **合并候选**：仅引用原稿的组合；同一组件的两个变体不能同入一个候选（409）。
- **签发约束**（全部满足才放行，否则 422 并列出全部违例）：
  - 每个组件（含传递依赖）存在覆盖目标地区、处于有效期且未撤回的授权；
  - 依赖的指纹钉扎匹配；
  - 并行评审：每个（评审人, 范围）以最新意见为准，无生效中的拒绝意见，
    且目标地区的通过人数达到地区策略要求（默认 1 人）；
  - 有效授权声明的适用标准覆盖地区策略要求的全部标准。
- **撤回授权**：授权行保留（`status=withdrawn`），未来签发被阻断；受影响的已发布
  版本标记为 `at_risk` 并记录事件，历史不删除。授权到期同理（读取时惰性标记）。
- **回滚**：版本置为 `rolled_back`，事件留痕，可修正后重新签发为新版本。
- **幂等与并发**：上传、合并、签发、撤回、回滚均可安全重放；并发签发同一候选
  只产生一个版本（唯一约束 + 事务串行化）。
- **重启恢复**：所有状态存于 SQLite，关闭后重开即恢复。

## 运行

```bash
python3 -m service_09252_004
# 环境变量：COURSEHUB_DB（默认 ~/.local/share/service_09252_004/coursehub.db）
#           COURSEHUB_HOST（默认 127.0.0.1）/ COURSEHUB_PORT（默认 8080）
```

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/components` | 上传组件元数据（指纹去重，201/200） |
| GET | `/components/{id}` | 查询组件 |
| POST | `/variants` | 上传语言变体（字幕、案例） |
| GET | `/variants/{id}` | 查询变体 |
| POST | `/licenses` | 登记版权授权（地区、标准、有效期） |
| GET | `/licenses` `?component_id=` | 列出授权 |
| GET | `/licenses/{id}` | 查询授权（含派生状态 expired/withdrawn） |
| POST | `/licenses/{id}/withdraw` | 撤回授权：阻断签发并标记风险版本 |
| POST | `/dependencies` | 登记组件依赖（拒绝成环，可钉扎指纹） |
| PUT | `/regions/{region}/policy` | 设置地区约束（必需标准、最少通过人数） |
| GET | `/regions/{region}/policy` | 查询地区约束 |
| POST | `/candidates` | 合并候选（同组件多变体 → 409） |
| GET | `/candidates/{id}` | 查询候选（含有效评审意见） |
| GET | `/candidates/{id}/check` | 预检：列出当前未满足的约束 |
| POST | `/candidates/{id}/reviews` | 提交审核意见（并行评审，可改判） |
| POST | `/releases` | 签发（约束未满足 → 422 + violations） |
| GET | `/releases` `?status=&region=` | 列出版本 |
| GET | `/releases/{id}` | 查询版本（含状态变迁事件） |
| POST | `/releases/{id}/rollback` | 回滚版本（历史保留） |
| GET | `/health` | 健康检查 |

典型流程：`POST /components` → `POST /variants` → `POST /licenses` →
`PUT /regions/EU/policy` → `POST /candidates` → `POST /candidates/{id}/reviews`
（多人并行）→ `GET /candidates/{id}/check` → `POST /releases`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：冲突决议（合并冲突、评审改判）、授权到期与撤回（阻断 + 风险标记 + 历史保留）、
并发签发（单胜者、多候选并行、上传去重）、依赖与地区标准约束、回滚重签、重启恢复。

## 编译检查

```bash
python3 -m compileall -q service_09252_004 tests
```
