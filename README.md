# 数字教学资源联合发布

本项目用于建设面向业务人员的纯服务端系统。代码按领域模型、应用服务、持久化与接口边界组织；时间、标识和外部输入应通过可替换端口接入，以便稳定复现状态变化。运行数据与本地配置不得写入源码目录。

## 架构

```
service_09252_004/
  domain.py    领域模型：组件、语言变体、授权、候选、评审、发布；内容指纹
  service.py   应用服务：合并候选、并行评审、签发约束、发布/回滚、授权撤回
  storage.py   持久化：SQLite 模式、线程本地连接、写事务（重启即恢复）
  api.py       接口边界：标准库 HTTP JSON API，错误码映射
  ports.py     可替换端口：时钟与标识生成（测试注入确定性实现）
run_server.py  服务入口（数据文件默认写入 cwd/var/，可用 --db 指定）
```

核心规则：

- **原稿不可变**：变体只增不改；合并候选通过引用组合各方内容，平台从不覆盖原稿。
- **签发约束**：目标地区的版权授权（地区/标准/有效期）、变体适用标准、依赖闭包、
  评审法定人数（默认 2，同一评审人以最新意见为准）全部满足才允许签发。
- **授权撤回**：阻断未来签发；包含该授权的已发布版本标记 `at_risk` 并记录原因，
  发布、快照与授权记录一律保留，绝不静默删除。授权到期同理阻断并在读取时动态标记。
- **内容指纹**：变体按 SHA-256 指纹去重（幂等上传）；发布按指纹幂等（重复签发返回同一发布）。
- **并发签发**：进程内写锁 + `BEGIN IMMEDIATE` + 部分唯一索引，保证同一课程/地区/标准
  恰有一个进行中的发布；新签发取代旧发布（旧版本保留为 `superseded`）。

## 运行

```bash
python3 run_server.py --host 127.0.0.1 --port 8080 --db ./var/releases.db
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/components` | 创建资源组件 |
| GET | `/components/{id}` | 组件详情（含变体与依赖） |
| GET | `/components/{id}/conflicts` | 同语言多版本冲突列表 |
| POST | `/components/{id}/variants` | 上传变体元数据与内容（指纹去重） |
| POST | `/components/{id}/dependencies` | 登记组件依赖（拒绝成环） |
| POST | `/licenses` | 授予版权授权（地区/标准/有效期） |
| POST | `/licenses/{id}/withdraw` | 撤回授权并标记受影响发布 |
| POST | `/candidates` | 建立合并候选（显式选择变体） |
| GET | `/candidates/{id}` | 候选详情与签发约束评估报告 |
| POST | `/candidates/{id}/reviews` | 提交评审意见（可并行） |
| POST | `/candidates/{id}/publish` | 签发发布（幂等） |
| GET | `/releases` · `/releases/{id}` | 发布列表/详情（含风险标记） |
| POST | `/releases/{id}/rollback` | 回滚进行中的发布（历史保留） |

错误约定：`{"error": {"code", "message", "details"}}`；
`not_found`→404，`conflict`→409，`validation_failed`→422。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

```bash
python3 -m compileall -q service_09252_004 tests
```
