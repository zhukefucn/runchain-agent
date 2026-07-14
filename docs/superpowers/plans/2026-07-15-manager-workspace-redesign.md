# Manager Workspace Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a润辰科技-branded, Codex-style manager workspace whose default is a real single AgentScope agent and whose reception expert team is an explicit demonstration mode.

**Architecture:** Persist the mode in the existing `SessionRecordRow.agent_id` using an allowlisted `general-assistant` or `reception-leader`. The manager chat endpoint dispatches server-side to a new general runtime or the existing reception runtime; the frontend renders the same stable event protocol in a three-column shell with a per-session result drawer.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, AgentScope 2.0.4, Vue 3, Pinia, TypeScript, Vitest, Playwright, PowerShell on Windows.

## Global Constraints

- Windows Phase 1 uses one FastAPI listener on `127.0.0.1:8000`; no Docker.
- Manager business data and AgentScope storage remain owner-scoped.
- Ordinary Agent and expert team may use only the current manager's authorized Skill/MCP capabilities.
- Expert-team business content remains Mock; crawler/search/OCR/Dify remain Phase 2.
- Unknown `agent_id` values fail closed.

---

### Task 1: Brand Asset and Shell Identity

**Files:**
- Create: `frontend/src/assets/runchain-logo.png`
- Modify: `frontend/src/components/BrandLockup.vue`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/views/LoginView.vue`
- Modify: `frontend/index.html`
- Test: `frontend/tests/role-routing.spec.ts`

**Interfaces:**
- Produces: reusable `<BrandLockup subtitle="..." />` component.

- [ ] Write a failing rendering test asserting the image accessible name `润辰科技` and absence of visible `RunChain` text.
- [ ] Run `pnpm --dir frontend test --run tests/role-routing.spec.ts`; expect the new assertion to fail.
- [ ] Copy the approved PNG without transformation, add `BrandLockup.vue`, replace both existing brand blocks, and change the document title to `润辰科技多租户智能体`.
- [ ] Re-run the targeted test; expect PASS.
- [ ] Commit with `feat: apply runchain technology branding`.

### Task 2: Server-Side Agent Mode Allowlist and Dispatch

**Files:**
- Create: `backend/app/agents/general.py`
- Modify: `backend/app/agents/__init__.py`
- Modify: `backend/app/api/manager.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/integration/test_role_apis.py`
- Test: `backend/tests/smoke/test_real_model.py`

**Interfaces:**
- Produces: `GENERAL_AGENT_ID = "general-assistant"`, `RECEPTION_AGENT_ID = "reception-leader"`.
- Produces: `GeneralAgentRuntime.chat(owner_user_id, session_id, prompt, request_id) -> AsyncIterator[StableEvent]`.
- Consumes: AgentScope `ChatService._run_impl`, `SQLiteStorage`, `ManagerLocalWorkspaceManager`, and existing stable SSE event schema.

- [ ] Add failing integration tests: session creation defaults to `general-assistant`, rejects an unknown ID with 422, and dispatches general/reception sessions to different runtime fakes.
- [ ] Run the focused integration tests; expect failures on current default/unconditional reception dispatch.
- [ ] Implement the constants and Pydantic allowlist using `Literal`.
- [ ] Implement `GeneralAgentRuntime`: lazily upsert a manager-owned `AgentRecord` and `SessionConfig`, call the pinned AgentScope chat implementation with `UserMsg`, read the new assistant message, and yield `run_started`, `token`, `complete` or a stable `error` event.
- [ ] Register `app.state.general_runtime`; in `/chat`, load the owned session once and dispatch exclusively from its persisted `agent_id`.
- [ ] Extend the real-model smoke test to call `/api/manager/sessions` with `general-assistant` and assert a non-empty assistant token/complete event.
- [ ] Run focused backend integration tests; expect PASS.
- [ ] Commit with `feat: add general agentscope manager mode`.

### Task 3: Mode-Aware Chat Store

**Files:**
- Modify: `frontend/src/stores/chat.ts`
- Test: `frontend/tests/manager-chat.spec.ts`

**Interfaces:**
- Produces: `type AgentMode = "general-assistant" | "reception-leader"`.
- Produces: `create(title: string, agentId?: AgentMode)` and `createForMode(agentId: AgentMode)`.
- Produces: per-session `resultOpen` state and `setResultOpen(open: boolean)`.

- [ ] Add failing tests asserting first send creates `general-assistant`, expert entry creates `reception-leader`, and structured events open only that session's result panel.
- [ ] Run `pnpm --dir frontend test --run tests/manager-chat.spec.ts`; expect failures.
- [ ] Change the default create/first-send path to `general-assistant`; retain `reception-leader` only for explicit expert creation.
- [ ] Store right-panel visibility in each `SessionState`; automatically open on `complete.plan` or new files, not plain token text.
- [ ] Re-run the targeted test; expect PASS.
- [ ] Commit with `feat: add manager conversation modes`.

### Task 4: Codex-Style Three-Column Manager Workspace

**Files:**
- Create: `frontend/src/components/ManagerSidebar.vue`
- Create: `frontend/src/components/ResultPanel.vue`
- Modify: `frontend/src/views/ManagerView.vue`
- Modify: `frontend/src/styles.css`
- Test: `frontend/tests/manager-chat.spec.ts`

**Interfaces:**
- `ManagerSidebar` emits `new-general`, `new-expert`, and `select(sessionId)`.
- `ResultPanel` accepts current plan/files/events and emits `close`.

- [ ] Add failing UI tests for default ordinary-Agent heading, expert-team button, session mode labels, result toggle, and three result tabs.
- [ ] Run the manager UI test; expect failures against the fixed reception layout.
- [ ] Implement the left sidebar with new chat, history, and two capability modes.
- [ ] Refactor the center header/composer copy to follow current session mode; keep expert roster visible only in expert mode.
- [ ] Implement the right panel tabs `业务结果`, `文件`, `执行详情`, with desktop collapse and mobile drawer behavior.
- [ ] Update responsive CSS so the center stays primary and both side regions become drawers on narrow screens.
- [ ] Re-run manager tests and `pnpm --dir frontend run typecheck`; expect PASS.
- [ ] Commit with `feat: redesign manager agent workspace`.

### Task 5: End-to-End Verification and Delivery

**Files:**
- Modify: `frontend/tests/e2e/demo-flow.spec.ts`
- Modify: `docs/demo-guide.md`

**Interfaces:**
- Verifies the public workflow; adds no production API.

- [ ] Update the E2E flow to log in, start an ordinary Agent conversation, create an expert-team conversation, toggle the result panel, and confirm manager isolation.
- [ ] Run backend tests, 21+ frontend tests, typecheck, production build, and Playwright E2E via `scripts/test.ps1`; expect all PASS.
- [ ] Run the opt-in Step model smoke test with `RUN_REAL_MODEL_TESTS=1`; expect both general and expert paths PASS.
- [ ] Reset demo data, start the Windows service, and manually verify the approved browser flow.
- [ ] Update the demo guide with the new default mode and expert-team entry.
- [ ] Commit with `test: verify redesigned manager workspace` and push the feature branch.
