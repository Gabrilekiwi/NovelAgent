# DeterminFlow 架构分析与 NovelAgent 未来方向

> 日期：2026-08-27
>
> 目的：记录 NovelAgent 团队对 DeterminFlow 项目的研究结论，以及该项目对 NovelAgent 架构演进的影响。

---

# 1. 项目定位

DeterminFlow 不是小说生成 Agent，而是一个面向生产的 AI Workflow Runtime。

核心理念：

> 让不确定的模型，运行在确定的流程里。

它解决的问题包括：

- Agent Workflow 编排
- 节点级上下文隔离
- Workflow Snapshot
- Checkpoint
- Retry / Recovery
- Token Ledger
- Script 与 Agent 分离
- Plugin 扩展
- API / Automation / Service 化交付

因此 DeterminFlow 更接近 AI 应用运行时基础设施。

---

# 2. DeterminFlow 与 NovelAgent 的关系

二者不是竞争关系，而是不同层级的问题。

## DeterminFlow 负责

AI 系统如何可靠运行：

```
Workflow
  ↓
Node
  ↓
Agent Execution
  ↓
Checkpoint
  ↓
Recovery
```

## NovelAgent 负责

长篇小说世界如何保持一致：

```
StoryProject
  ↓
Semantic Authority
  ↓
World State
  ↓
Character State
  ↓
Timeline
  ↓
Continuity Validation
```

NovelAgent 的核心竞争力不是 Workflow，而是 Story State Engine。

---

# 3. 双方优势对比

## DeterminFlow 优势

### 3.1 Runtime 能力

强项：

- Workflow Runtime
- Node 生命周期管理
- Failure Recovery
- Context Isolation
- Token Tracking
- Plugin System

它解决的是：

"如何让复杂 AI 流程稳定运行。"

---

## NovelAgent 优势

### 3.2 小说领域状态管理

NovelAgent 已经建立：

- StoryProject Semantic Authority
- Field Provenance
- Memory V2
- Authoritative State
- Scene Evidence
- Final Artifact Integrity
- Chapter Recovery
- Continuity Validation

它解决的是：

"如何让一本 500 章小说的世界状态长期可信。"

---

# 4. 从 DeterminFlow 学到的设计方向

## 4.1 引入 Runtime Layer

未来 NovelAgent 不应该重造通用 Workflow Engine。

应该增加独立 Runtime 层：

```
NovelAgent

Story Intelligence Layer

---------------------

Agent Runtime Layer

---------------------

Models / Tools / Storage
```

---

## 4.2 Node 抽象

未来可以统一：

```
GenerationNode
ValidationNode
RepairNode
StateUpdateNode
MemoryNode
DeliveryNode
```

每个 Node 拥有：

- 输入
- 输出
- Context
- Retry Policy
- Execution State
- Token Usage

---

## 4.3 统一 Recovery State Machine

当前 NovelAgent 已有：

- locked chapter recovery
- scene recovery
- provider recovery
- repair recovery

未来应该统一为：

```
ExecutionStateMachine

Node
Status
Checkpoint
Retry
Recovery
Evidence
```

---

## 4.4 Context Isolation

未来不同 Agent 不应该共享无限上下文。

应该变成：

```
Director Context
Writer Context
Validator Context
Repair Context
State Context
```

每个角色只读取自己需要的信息。

---

## 4.5 Token Ledger

未来需要记录：

```
Chapter
 ├ Director Token
 ├ Planning Token
 ├ Writing Token
 ├ Validation Token
 ├ Repair Token
 └ State Update Token
```

用于成本分析和 Prompt 优化。

---

# 5. 不应该吸收的方向

NovelAgent 不应该变成通用 Workflow Engine。

原因：

如果过度向 DeterminFlow 靠拢，会削弱 NovelAgent 最有价值的部分：

- Narrative State
- Story Authority
- Continuity
- World Simulation

未来方向不是：

```
NovelAgent = another Workflow Engine
```

而是：

```
NovelAgent = Story Intelligence Engine running on AI Runtime
```

---

# 6. 长期架构方向

最终目标：

```
                NovelAgent

        Story Intelligence Engine

        - World Model
        - Character Model
        - Timeline Model
        - Narrative Memory
        - Continuity Engine

-------------------------------

        AI Workflow Runtime

        - Nodes
        - Execution
        - Recovery
        - Observability

-------------------------------

        Models / Tools / APIs
```

---

# 7. 战略判断

DeterminFlow 证明：

多 Agent + Workflow + Runtime + Recovery 是未来 AI 应用的重要基础设施。

NovelAgent 当前路线仍然正确，但未来应该吸收 Runtime 思想，而不是替换 Story Engine。

未来最佳组合可能是：

```
DeterminFlow Runtime
        +
NovelAgent Story Engine
        +
oh-story 创作能力
        +
ComfyUI / 视频生成链路
```

形成从：

小说 → 世界状态 → 动画 → 视频 → 内容生产

的完整 AI 创作系统。
