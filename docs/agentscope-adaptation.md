# AgentScope 适配说明

## 1. 适配原则

本项目不 fork AgentScope，也不把 QwenPaw 改造成多租户应用。AgentScope 源码以同级目录 `agentscope-main` 的 editable dependency 使用；RunChain 通过正式扩展点和少量边界适配完成鉴权、存储、Workspace、动态 Tool 与稳定前端 API。当前源码包没有 Git 元数据，因此 `agentscope-source.lock.json` 以发布版本 `2.0.4` 加完整源码树 SHA-256 代替不可验证的 commit 声明；安装和环境检查都会验证该锁，且不会改写上游 AgentScope 树。

QwenPaw 仅用于参考 `SKILL.md`、有效 Skill 解析、Agent 构建和治理交互思想，不作为运行时依赖。

## 2. 嵌入方式

根 FastAPI Gateway 创建并挂载 AgentScope `create_app()`：

```text
RunChain FastAPI
├── /api/auth/*
├── /api/manager/*
├── /api/business/*
├── /api/system/*
├── /api/health、/api/ready
└── /internal/agentscope/*  -> AgentScope create_app
```

传入的实例级依赖包括：

- `SQLiteStorage`
- AgentScope `InMemoryMessageBus`
- `ManagerLocalWorkspaceManager`
- `extra_agent_tools` 异步工厂
- 自定义 runtime Agent class
- 三个 `custom_subagent_templates`

这些对象由 app factory 创建，不使用跨应用共享的可变运行时单例。AgentScope 子应用 lifespan 由根应用显式进入一次，关闭时与 MCP、数据库资源按顺序清理。

## 3. 身份适配

AgentScope 原生服务需要 `get_current_user_id`。RunChain 不允许浏览器直接声明该值，而是：

1. Gateway 验证 Bearer JWT、数据库用户启用状态和 token/数据库角色一致性。
2. 仅 `manager` 可以访问 `/internal/agentscope`。
3. 将数据库中的真实 user ID 写入 request state。
4. 覆盖 AgentScope 身份依赖，使其只读取该 verified state。
5. 在 ASGI 层删除 `X-User-ID`、`X-Owner-User-ID`、`X-Role` 和同名 query 参数，HTTP 与未来 WebSocket 共用策略。

管理员的治理权限不会被转换成 manager 业务身份。因此 business_admin/system_admin 不能借 AgentScope 内部路由读取会话或创建业务 Agent。

## 4. Storage 适配

`backend/app/agentscope_ext/sqlite_storage.py` 实现当前 AgentScope `StorageBase` 契约，将 Session、Agent state、Team、message 等结构持久化到项目的 SQLAlchemy/SQLite 数据模型。

关键约束：

- 所有 session key 包含 user/owner；owner 不匹配时不返回记录。
- AgentScope 存储和 Gateway manager Repository 使用同一业务所有权边界。
- SQLite 使用异步会话、foreign keys 和 WAL；不跨线程共享裸 cursor。
- 团队创建失败时保留可能已生成的 AgentScope artifact 供审计，不做可能影响重试或共享状态的盲目删除。

## 5. Workspace 适配

AgentScope 原生 Local Workspace 的目录规则不足以表达本 DEMO 的 manager 隔离，因此 `ManagerLocalWorkspaceManager` 把 user ID 放入物理路径，并在解析 Workspace 前校验 session owner/agent。

每条路径必须满足：

- 来自已验证 manager 和属于他的 session；
- 规范化后仍处于该 manager 根目录；
- 不含绝对路径或 `..`；
- 不经过符号链接、junction/reparse point 逃逸；
- 不因已知另一个 manager 的 session ID 而切换目录。

这是安全必要的适配，不是 UI 层的命名约定。

## 6. 模型适配

AgentScope ChatService 会实例化 Agent。RunChain 传入 app-instance runtime Agent class，将 AgentScope session 中占位/外部提供的模型替换为根应用已经构造的模型：

- `APP_ENV=test`：`DeterministicFakeModel`，无网络、输出确定。
- `APP_ENV=real`：`OpenAIChatModel` + `OpenAICredential`，模型为 `step-3.7-flash`，OpenAI-compatible Base URL。

这保证挂载后的原生 Chat 路由与项目自建 Agent factory 使用相同模型边界。API Key 只在构造 credential 时解封；readiness 和 system admin 页面只读取 `configured` 等非敏感状态。

## 7. 动态 Toolkit 适配

`extra_agent_tools(user_id, agent_id, session_id)` 在 Agent 构建时调用 `AuthorizedToolService`。它不会返回全局共享 Toolkit，而是每次完成以下校验：

1. user 是启用的 manager；
2. session 的 owner、agent 和 active 状态全部匹配；
3. Skill 已发布、已授权且版本/文件哈希有效；
4. MCP Server 已授权、处于运行状态且 Runtime Registry 会话健康；
5. Tool schema 有大小、深度和节点限制；
6. Tool 名称规范化后不与 AgentScope 保留名或其他 Tool 碰撞。

Python Tool、prompt Tool 和 MCP Tool 的 callback 都闭包捕获验证后的 manager/session。即使模型生成了其他 `owner_user_id` 参数，也不能改变真实执行身份。

## 8. Team 与 SubAgentTemplate

`reception_subagent_templates()` 注册 `pickup`、`lodging`、`dining` 三种 AgentScope `SubAgentTemplate`。接待 runtime 使用适配后的 TeamCreate/AgentCreate 工具创建真实 AgentScope Team 与成员，并将 Team ID/worker ID、节点运行和 HITL 状态写入持久化层。

Phase 1 的接待业务语义仍是 Mock，但受治理的执行路径是真实的：runtime 优先查找当前 manager 已授权的运行中 MCP Server，并通过 `McpService.call_tool()` 执行接站；同时优先查找已发布、已授权的 Python Skill，并通过 Controlled Runner 执行餐饮。只有缺少相应授权能力时才回退到 MCP-client-shaped 和 controlled-runner-service-shaped 确定性适配器，住宿保持进程内 Mock Tool。HTTP 对话的 `request_id` 会透传到 governed provider、Skill/MCP 调用及审计记录。这样既能在全新环境稳定展示并发编排、失败和 HITL，也能证明两个 manager 的真实授权与隔离链路；Phase 2 可替换模板和 Tool 配置，不需要改变 manager 隔离核心。

## 9. SSE 稳定化

AgentScope/Team 内部事件先转换为项目的 `StableEvent`，再由 `/api/manager/sessions/{id}/chat` 输出 `text/event-stream`。事件集合固定为：

```text
run_started token agent_started tool_call tool_result
agent_completed hitl_pending complete error
```

每个事件至少包含 `request_id`、`session_id`、`run_id`、`timestamp`、`data`。前端只依赖这一稳定层，不直接绑定 AgentScope 内部事件类。SSE 断开会终止当前流并持久化取消/终态，不删除已经提交的会话和运行记录。

## 10. 升级 AgentScope 时的检查清单

- 核对 `create_app()` 的 Storage、MessageBus、Workspace、Tool factory、Agent class 和 subagent template 参数。
- 运行 SQLiteStorage contract tests，确认复合 owner key 没有退化。
- 运行挂载后 Chat/SSE 身份测试，包括伪造 Header/Query 和 admin 拒绝。
- 运行 Workspace 跨 manager、路径穿越和 reparse point 测试。
- 运行 TeamCreate/AgentCreate、并发节点、HITL 恢复和取消清理测试。
- 确认 AgentScope 新增保留 Tool 名已加入碰撞拒绝集合。
- 确认 shutdown/cancellation 下子应用、MCP Registry、DB 的关闭顺序仍成立。
