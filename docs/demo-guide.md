# 浏览器演示手册

本手册严格对应 Spec v1.2 第 14.1 节的 11 步主演示。接待业务结果全为 Mock；框架、隔离、Skill/MCP 治理、SSE、HITL、持久化和审计链路是真实运行。

经理登录后的默认首页是“普通 Agent”对话工作台：左侧为能力与历史会话，中间为对话，右侧“结果”面板按需打开。首次直接发送消息会自动创建普通 Agent 会话并调用真实配置模型；“接待专家团（演示）”是左侧的显式可选能力，不是默认首页。

## 演示前准备

1. 按 [README](../README.md) 完成 `.env`、安装和启动，确认前端 <http://127.0.0.1:8000/>、`/api/health` 和 `/api/ready` 可访问。Windows 演示拓扑只有 FastAPI 的 8000 端口，前端使用构建后的静态文件，不另开 Vite 5173 端口。
2. 准备两个本机 Skill ZIP：

```powershell
Compress-Archive -Path .\demo-skills\reception-dining -DestinationPath "$env:TEMP\reception-dining.zip" -Force
Compress-Archive -Path .\demo-skills\private-demo -DestinationPath "$env:TEMP\private-demo.zip" -Force
```

ZIP 必须保留单一 Skill 根目录，不能只压缩根目录内的散文件。

3. 如需现场登记 MCP，在 PowerShell 执行 `(Resolve-Path .\.venv\Scripts\python.exe).Path` 并复制绝对路径。浏览器中 MCP 参数填写 `mock_pickup_server.py`；Phase 1 不接受任意命令、额外参数或环境变量。
4. 建议使用一个普通窗口和一个无痕窗口，方便比较两个 manager。每次切换角色也可以点击右上角“退出”。

## 四个账号

| 用户名 | 工作台 | 密码 |
|---|---|---|
| `manager0001` | `/manager` | `12345678` |
| `manager0002` | `/manager` | `12345678` |
| `business_admin01` | `/business` | `12345678` |
| `system_admin01` | `/system` | `12345678` |

## 精确 11 步演示流程

### 1. system_admin 登录，展示四个用户和模型已配置状态

使用 `system_admin01 / 12345678` 登录。先在“用户与角色”确认四个种子账号、三种角色和启用状态，并记下两个 manager 表格中显示的用户 ID；再进入“模型状态”，确认模型名、脱敏 Base URL 和“已配置”。页面不会显示 API Key。

验收点：系统管理员只能看到用户、模型摘要和审计元数据，页面没有 manager 会话正文或 Workspace 文件入口。

### 2. business_admin 登录，从本机上传演示 Skill ZIP

退出后用 `business_admin01 / 12345678` 登录。在“Skill 管理”选择 `%TEMP%\reception-dining.zip`，点击“安装 Skill”。

验收点：入口是浏览器本机文件选择器；没有 URL、GitHub、在线市场或服务器下载入口。

### 3. 展示安装检查，发布 Skill，并授权给两名 manager

查看 Skill 类型、版本、状态和静态检查告警。点击“发布”，然后在“经理用户 ID”中分别输入步骤 1 记下的 manager ID，各执行一次授权。

可切换到“MCP Server”，填写：显示名称 `mock-pickup`；命令为准备阶段复制的 `.venv\Scripts\python.exe` 绝对路径；参数为 `mock_pickup_server.py`。登记后点击“测试连接”，展示健康状态与 Tool 列表；演示结束可点击“停止”显式关闭该 Server，页面状态随即回到 `stopped`。

验收点：发布与授权是两个独立动作；两名 manager 都获得相同基础能力。

### 4. manager0001 登录，显式选择并运行接待专家团

使用 `manager0001 / 12345678` 登录。先展示默认“润辰智能助手”普通对话，再点击左侧“接待专家团（演示）”，输入示例需求：

> 明天 18:00 有 4 位远方客人到站，请安排接站、两间房和清淡晚餐，预算 1000 元。

点击“发送”。

验收点：新会话出现在左侧，当前 manager 的消息和运行记录进入自己的隔离空间。

### 5. 展示三个子 Agent 的并行时间线、MCP/Python Tool 调用和 HITL

点击顶部“结果”，再进入右侧“执行详情”：接待主管拆解接站、住宿、餐饮三个节点；时间线显示 Agent start/complete 以及 Tool call/result；主区域最终出现“需要人工确认”卡片。

在“文件”页签展示当前 manager 已授权 Skill 和自己的文件。接待路线、酒店、餐饮内容是 Mock，但 Team、并发节点、事件和暂停状态是真实持久化。

### 6. manager0001 确认后得到最终 Mock 接待方案

点击“确认方案”。也可以在补充演示中选择“修改”并提交意见，或选择“取消”；同一个 HITL 只能接受一致的幂等决策。

验收点：时间线恢复，出现 `complete`，右侧“业务结果”自动展示最终方案；刷新页面后，会话、消息和完成状态仍可恢复。

### 7. manager0002 登录，独立完成同样流程，证明其可正常运行

退出后使用 `manager0002 / 12345678` 登录。点击“接待专家团（演示）”新建专家团会话，输入另一组接待条件，等待 HITL 并确认。

验收点：manager0002 不是只读或降级账号；它可以独立完成专家团、Tool、HITL 和最终方案全流程，且事件中不出现 manager0001 的 session/run。

### 8. business_admin 将“专属演示 Skill”只授权给 manager0001

切回 `business_admin01`。上传 `%TEMP%\private-demo.zip`，查看检查结果并发布。只输入 manager0001 的用户 ID 执行授权，不给 manager0002 授权。

验收点：业务管理员可查看治理状态和调用元数据，但看不到两个 manager 的会话正文。

### 9. 展示 manager0002 看不到/不能调用该 Skill

重新以 `manager0002` 登录，在右侧“结果 → 文件 → 已授权 Skill”确认没有 `private-demo`。尝试让 Agent 使用该专属能力时，后端不会把未授权 Tool 注入其 Toolkit；直接构造资源 ID 的未授权调用也必须失败并产生审计。

随后以 `manager0001` 登录，确认 `private-demo` 可见，形成同一发布版本、不同 manager 授权集合的对照。

### 10. 展示 manager0002 看不到 manager0001 的会话和文件

在 manager0001 工作台记下会话标题，并查看“结果 → 文件”。切回 manager0002，确认左侧列表没有该会话，文件页也没有 manager0001 文件。

验收点：即使已知其他 manager 的资源 ID，后端 GET/UPDATE/DELETE 仍返回 404/403，不返回资源正文；仅凭浏览器 UI 不显示不算完整安全边界，自动隔离测试同时覆盖直接访问。

### 11. system_admin 查看对应审计元数据

以 `system_admin01` 登录，进入“审计日志”。展示 Skill 安装、发布、授权、manager 会话/运行、拒绝访问或未授权调用等记录，通过 request ID 关联操作。

验收点：审计包含 actor、action、resource、result、request ID 和脱敏 details，不复制会话正文、文件内容、原始 traceback 或密钥。

## 演示结果记录

完成一次人工浏览器演示后更新下表；不得在没有实际执行时填写“通过”。

| 日期 | 环境 | 结果 | 操作者 | 备注 |
|---|---|---|---|---|
| 2026-07-14 | Windows 本机 / Chromium | 通过 | Codex（人工浏览器复验 + Playwright） | 干净启动后人工确认：manager 初始会话为空，创建并完成主管/接站/住宿/餐饮协作、工具轨迹、HITL 审批与最终方案；公开界面无内部 worker、owner 或 AgentScope ID，760px 执行详情抽屉可用。Playwright 另行覆盖四账号、Skill 上传/发布/双 manager 授权与受控 Runner 真实执行、本机 MCP 双授权与两次真实 `CallTool`、私有 Skill 成功/拒绝、跨 manager 读取拒绝，以及 system_admin 停用/登录拒绝/重新启用和对应审计。StepFun Smoke 通过挂载的 AgentScope Chat API 获得真实非空回复。 |
| 2026-07-15 | Windows 本机 / 内置浏览器 | 通过 | Codex | 实际登录 manager0001，确认润辰科技品牌、普通 Agent 默认首页、首条消息自动建会话，并通过公共 manager API 获得 StepFun 真实回复；确认专家团为显式入口、左右能力/会话与右侧结果抽屉可用。343 项后端离线测试、24 项前端测试、类型检查和生产构建通过。 |

## 常见问题

- 需要从干净状态重新演示：运行 `scripts/reset-demo.ps1`，再运行 `scripts/start.ps1 -SkipSetup`。该操作仅清除本项目运行数据，保留 `.env` 与 `demo-skills/`。

- `/api/ready` 不通过：先运行 `scripts/check-env.ps1`，检查端口、目录权限、SQLite、Runner 能力和模型变量。
- Skill ZIP 被拒绝：确认 ZIP 内只有一个根目录，根下直接包含 UTF-8 `SKILL.md`、合法 `skill.json` 和 manifest 声明的入口文件。
- MCP 登记失败：命令必须是当前 `.venv` Python 的规范化绝对路径，参数只能是 allowlist 中的 `mock_pickup_server.py`，环境变量必须为空。
- 看不到新授权：重新创建或重新进入 Agent 会话使 Toolkit 按最新授权构建；不需要重启整个服务。
- 真实模型失败但离线测试通过：外部服务连通性不属于离线套件；检查 `.env`、网络和模型服务状态，不要把密钥输出到终端。
