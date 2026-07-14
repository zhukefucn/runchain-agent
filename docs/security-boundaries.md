# 安全边界

## 1. 适用范围与威胁模型

Phase 1 是本机技术 DEMO，面向受信任的开发/演示人员和经过业务管理员审核的 Skill/MCP 配置。它展示多用户业务数据隔离与治理控制，但不应直接暴露到公网，也不满足生产银行系统、恶意插件托管或多主机零信任环境的安全要求。

最重要的限制：**Windows Controlled Python Runner 不是恶意代码强沙箱。** Python Skill 最终以启动 DEMO 的 Windows 账号权限运行，仍可能拥有该账号可访问的文件和网络能力。只能安装已知来源、已审查的 Skill。

## 2. 角色和数据可见性

| 能力 | manager | business_admin | system_admin |
|---|---:|---:|---:|
| 自己的会话、消息、运行、Workspace | 是 | 否 | 否 |
| 其他 manager 的业务内容 | 否 | 否 | 否 |
| 运行 Agent、专家团和自己的 HITL | 是 | 否 | 否 |
| 使用已发布且授权的 Skill/MCP | 是 | 否 | 否 |
| 上传、发布、停用和授权 Skill | 否 | 是 | 否 |
| 登记、测试本机 MCP | 否 | 是 | 否 |
| 用户、角色、启停 | 否 | 否 | 是 |
| 模型非敏感状态、全局审计元数据 | 否 | 否 | 是 |

Phase 1 的 `tenant_id` 固定为单银行 `bank_demo`。真实业务隔离点是 manager 用户，不是 `tenant_001/tenant_002` 机构租户。

## 3. 认证、RBAC 与身份注入

- 密码使用 Argon2 哈希；初始 `12345678` 仅用于 DEMO，不得用于其他环境。
- JWT 使用 HS256，包含 user、role、tenant 和过期时间。生产化前必须采用足够随机的 secret、密钥轮换和更完善的会话撤销策略。
- 每次受保护请求都检查 token、数据库用户是否存在/启用，以及 token 角色与当前数据库角色是否一致。
- `user_id`、`owner_user_id`、`role` 等客户端字段、Header 和 Query 参数不参与授权。
- AgentScope 内部路由只允许 manager；business_admin/system_admin 的全局治理身份不能提升为业务身份。
- 前端路由守卫不是安全控制；所有边界由后端重复执行。

## 4. Manager 隔离

会话、消息、Team run、Team node、HITL、Workspace 文件和 Skill invocation 都带 `owner_user_id`。Repository 查询必须同时带当前 owner，子资源读取也要校验父 session/run 的 owner。

资源 ID 正确但 owner 不匹配时返回 404 或 403，不返回正文，不通过差异化错误泄露资源是否存在。两个方向的跨 manager 列表、直接 GET/UPDATE/DELETE、路径穿越、已知 HITL ID 和并发 SSE 均应由自动测试覆盖。

管理员默认只能查看治理/审计元数据。审计记录不能复制 manager 的 prompt、模型回复、文件内容或敏感输入。

## 5. Workspace 边界

- 目录包含 manager ID；session/agent 路径必须与数据库 owner 一致。
- 拒绝绝对路径、盘符路径、UNC、`..`、Windows 设备名和规范化后逃逸。
- 拒绝符号链接、junction/reparse point 穿越。
- manager 不能通过猜测另一个 session ID 获得其工作目录。

这防止应用接口层的跨 manager 文件访问，但不能阻止拥有同一 Windows 账号或管理员权限的本机操作者直接读取磁盘。生产部署必须将操作系统账号、目录 ACL、备份和磁盘加密纳入整体控制。

## 6. Skill 安装边界

唯一安装入口是 business_admin 的本机浏览器 ZIP 上传；没有 URL、GitHub、互联网市场或服务器主动下载入口。

安装器执行有界检查：上传大小、条目/文件数、单文件与总解压大小、路径深度、压缩比、单一根目录、重复/大小写别名、绝对/遍历路径、Windows 设备名、链接类型、UTF-8 `SKILL.md`、manifest Schema、严格语义版本和入口路径。安装保存包、内容、`SKILL.md` 和文件哈希，运行前再次校验有效发布版本和 manager 授权。

Python AST 扫描产生危险 import/call 告警，但静态告警不是安全证明，也不能检测所有动态行为。business_admin 必须把代码审核作为发布前置步骤。

## 7. Controlled Python Runner：提供与不提供的保护

Runner 提供：

- 每次调用一个独立 Python 子进程，不在 FastAPI 主进程 `exec()`；
- `python -I -X utf8`、独立临时 cwd、最小环境变量；
- 不传递模型 Key、JWT Secret、数据库 URL 等父进程敏感环境；
- 结构化 JSON 输入/输出，输入、stdout、stderr 和 JSON 复杂度上限；
- 默认 15 秒超时、256 MiB 内存、最多 4 个进程、进程树终止；
- Windows Job Object 的 kill-on-close、CPU 时间、内存和 active process 控制；
- runner 根 DACL 只允许当前 Windows 用户和 SYSTEM，并验证调用目录继承；
- 全局并发和排队超时；失败只返回摘要，不向 manager 暴露原始 traceback。

Runner **不提供**：

- 独立 Windows 用户、虚拟机或容器级文件系统隔离；
- 网络隔离或域名/IP allowlist；
- 对当前 Windows 账号可读文件的强制拒绝；
- 对本机同账号恶意进程的防护；
- 对哈希校验到进程打开入口文件之间 TOCTOU 的绝对消除；
- 可安全执行未知、恶意或未经审核 Python 的承诺。

默认不允许 Skill 自行安装依赖；Phase 1 只使用平台预装的白名单依赖。任何声称“可安全运行任意 Python”的演示说明都是错误的。

Phase 2 的明确替换边界是同一 `SkillExecutor` 协议下的 Ubuntu `DockerSandboxExecutor`。容器版本仍需只读镜像/文件系统、非 root、capability/seccomp、网络策略、CPU/内存/PID/磁盘配额、超时清理、镜像签名与漏洞管理；“使用 Docker”本身也不自动等于安全沙箱。

## 8. MCP 边界

- 只有 business_admin 可登记和测试 Server。
- Phase 1 只接受本机 stdio Python Server；命令必须是当前 canonical Python，可执行脚本必须位于配置的 MCP 根且在 allowlist 中。
- 拒绝任意 shell、相对/逃逸路径、额外参数、环境变量和 reparse point。
- MCP 进程不会自动继承模型密钥。
- Tool 只有在 Server 运行、Tool 已发现且当前 manager 已授权时才注入 Toolkit。
- Tool schema 和规范化名称受限；保留名或跨 Skill/MCP 碰撞时 fail closed。
- 健康检查、启动、Tool 发现、调用失败和关闭写入脱敏审计。

本机 MCP 仍是与 DEMO 主机同信任域的代码。Phase 2 接真实内部系统时需要单独的服务身份、出站网络策略、最小权限凭据、证书校验和调用级审计。

## 9. 密钥、模型和日志

- 模型 API Key 和 JWT Secret 只放本地 `.env` 或进程环境；`.env` 已被 Git 忽略。
- API Key 不写数据库、前端 bundle、测试快照、SSE、审计 details 或系统状态响应。
- system_admin 只能看到模型名、脱敏 Base URL、configured/connectivity 状态。
- Base URL 禁止嵌入 userinfo，real 模式要求 HTTPS（localhost 除外）。
- 真实模型测试仅在 `RUN_REAL_MODEL_TESTS=1` 时执行，不应打印 secret 或完整凭据对象。
- REST 错误使用 `code`、`message`、`request_id`；原始 traceback 和环境变量留在受控服务端日志，且日志仍应配置访问控制与保留周期。

如怀疑密钥泄漏：立即停止服务、轮换模型 Key/JWT Secret、删除本地 `.env` 与可能的日志/截图副本、检查 Git 历史和审计，并使旧 JWT 全部失效。

## 10. Windows DEMO 与未来 Ubuntu 的边界

Windows 版本明确不使用 Docker Desktop，因为当前环境无法运行它。它适合单机、受控人员、可信 Skill 的技术演示。

迁移 Ubuntu/Docker 前不能仅复制启动脚本，应重新完成：反向代理/TLS、CSRF/CORS 策略、Secret Manager、容器执行隔离、PostgreSQL/Redis 权限、备份恢复、日志脱敏、依赖/SBOM、漏洞扫描、速率限制、多实例并发与租户渗透测试。真实爬虫、搜索、OCR、Dify 和银行系统连接也必须经过单独威胁建模。

## 11. 演示前安全检查

- `.env`、SQLite、Workspace、Runner 临时目录未进入 Git。
- 页面、终端、截图和测试报告不含真实 API Key。
- 两个 manager 双向跨会话/文件/HITL/Skill 访问均失败。
- business_admin/system_admin 不能访问 manager API 或 AgentScope 内部业务路由。
- Runner DACL 和 Windows Job Object readiness 通过。
- MCP 只使用 allowlist Server 且未配置敏感环境变量。
- 演示人员明确说明接待业务为 Mock，Runner 不是恶意代码沙箱。
