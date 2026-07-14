# RunChain AgentScope 多租户 DEMO

这是一个直接运行在 Windows 上的 Phase 1 多租户 Agent 框架 DEMO。它将 AgentScope 嵌入 FastAPI Gateway，以 `manager` 用户作为真实业务隔离点，并提供 Skill 治理、受控 Python 执行、本机 MCP、接待专家团、SSE、HITL、SQLite 持久化和三角色浏览器工作台。

Phase 1 是通用技术底座，不是银行尽调或报告系统。接待专家团的路线、酒店、餐饮等业务结果全部为 Mock；多 Agent 编排、会话隔离、状态持久化、Skill/MCP 治理和 HITL 链路是真实实现。

## 演示账号

| 用户名 | 角色 | 密码 |
|---|---|---|
| `manager0001` | 客户经理 | `12345678` |
| `manager0002` | 客户经理 | `12345678` |
| `business_admin01` | 业务管理员 | `12345678` |
| `system_admin01` | 系统管理员 | `12345678` |

这些是 DEMO 初始凭据，只能用于本地演示。数据库保存密码哈希，不保存明文密码。

## Windows 快速启动

前置条件：Windows PowerShell、Python 3.11–3.13、Node.js、pnpm，以及位于本项目同级目录的 `agentscope-main` 源码。Windows 版本不需要、也不使用 Docker Desktop。Phase 1 当前锁定 AgentScope `2.0.4`；`agentscope-source.lock.json` 记录完整 `src/agentscope` 源码树摘要，`setup.ps1` 和 `check-env.ps1` 会在使用 editable dependency 前验证，且不会修改上游目录。

在本目录执行：

```powershell
Copy-Item .env.example .env
notepad .env
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\check-env.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

`.env` 至少配置随机的 `JWT_SECRET_KEY` 和本地开发用 `MODEL_API_KEY`。不要把密钥提交到 Git、复制到截图或粘贴到日志。启动脚本构建 Vue，并由 FastAPI 在同一个本机监听端口提供前端与 API。启动完成后访问：

- 前端：<http://127.0.0.1:8000/>
- 后端健康检查：<http://127.0.0.1:8000/api/health>
- 后端就绪检查：<http://127.0.0.1:8000/api/ready>

停止服务：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
```

脚本只停止 `.run/` 中记录的本 DEMO 进程。

需要恢复到初始演示数据时，可执行下面的可选命令，再重新启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\reset-demo.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1 -SkipSetup
```

重置脚本先按进程身份记录安全停止本 DEMO，只清除项目内的 SQLite、manager 工作区、已安装 Skill、Runner 与 E2E 运行状态；不会删除 `.env` 或 `demo-skills/` 源码夹具。

## 测试

离线完整测试使用确定性 Fake Model，不依赖外部模型服务：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test.ps1
```

该脚本依次运行后端 pytest、前端 Vitest、TypeScript 检查、生产构建和 Playwright E2E；可用 `-SkipE2E` 只在明确需要时跳过浏览器测试。

真实模型 Smoke Test 是显式选择项，不属于离线套件的通过条件：

```powershell
$env:RUN_REAL_MODEL_TESTS='1'
.\.venv\Scripts\python.exe -m pytest backend/tests/smoke/test_real_model.py -q -m real_model
Remove-Item Env:RUN_REAL_MODEL_TESTS
```

只有 `RUN_REAL_MODEL_TESTS=1` 时才会调用 `.env` 中配置的模型。测试不应输出 API Key，也不对模型自然语言正文做快照断言。

## Phase 边界

Phase 1 包含：四账号 JWT/RBAC、两个 manager 的完全业务隔离、AgentScope 会话/Team/Workspace、Skill ZIP 本机上传与授权、受控 Python Runner、本机 MCP、接待专家团 Mock 场景、SSE/HITL、SQLite、审计与 Windows 脚本。

Phase 1 不包含：真实爬虫、搜索、OCR、Dify、银行内部系统、尽调/DOCX 报告、机构组织树、互联网 Skill 安装入口，以及可安全执行恶意代码的强沙箱。

Phase 2 可在保持上层 API 基本稳定的前提下接入真实业务 Skill/MCP，并在 Ubuntu 上以 Docker Sandbox、PostgreSQL、Redis 等实现替换本地执行与存储后端。

## 文档

- [架构说明](docs/architecture.md)
- [11 步演示手册](docs/demo-guide.md)
- [AgentScope 适配说明](docs/agentscope-adaptation.md)
- [安全边界](docs/security-boundaries.md)
- 上层目录中的 `AgentScope多租户Agent系统DEMO-Spec-v1.2.md` 是 Phase 1 验收基线。

> 重要：Windows Controlled Python Runner 是面向已审核、可信 Skill 的受控运行器，不是针对恶意代码的操作系统级沙箱。详细限制见[安全边界](docs/security-boundaries.md)。
