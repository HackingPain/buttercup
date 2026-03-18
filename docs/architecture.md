# Buttercup Architecture

This document provides visual diagrams of the Buttercup CRS architecture, covering the high-level system design, data flow, message queue topology, and Kubernetes deployment layout.

## System Architecture

The following diagram shows all major components and how they communicate. The **Orchestrator** (task-server, downloader, scheduler) coordinates the overall workflow. **Redis** serves as the central message broker. **LiteLLM** proxies all LLM calls for the patcher and seed-gen components.

```mermaid
graph TB
    subgraph "External"
        CompAPI["Competition API<br/>(external)"]
        LLMProviders["LLM Providers<br/>(OpenAI / Anthropic / Google)"]
    end

    subgraph "Orchestrator"
        TS[Task Server]
        DL[Task Downloader]
        SCH[Scheduler]
    end

    subgraph "Fuzzing Pipeline"
        BB[Build Bot<br/>x4 replicas]
        FB[Fuzzer Bot]
        CB[Coverage Bot]
        TB[Tracer Bot]
    end

    subgraph "Analysis"
        PM[Program Model]
        SG[Seed Gen]
        PA[Patcher]
    end

    subgraph "Infrastructure"
        Redis[(Redis)]
        LiteLLM[LiteLLM Proxy]
        DinD[DinD Daemon]
        SigNoz[SigNoz / OTel]
    end

    subgraph "Storage"
        TaskVol[("tasks_storage<br/>(PVC, RWX)")]
        ScratchVol[("crs_scratch<br/>(PVC, RWX)")]
        NodeLocal[("node_data<br/>(hostPath)")]
    end

    CompAPI -- "POST /tasks" --> TS
    TS -- "TaskDownload msg" --> Redis
    DL -- "reads TaskDownload" --> Redis
    DL -- "downloads repo" --> TaskVol
    SCH -- "reads/writes all queues" --> Redis
    SCH -- "submits results" --> CompAPI

    BB -- "reads BuildRequest" --> Redis
    BB -- "writes BuildOutput" --> Redis
    BB -- "builds via Docker" --> DinD

    FB -- "runs fuzzers" --> DinD
    FB -- "writes Crash" --> Redis
    CB -- "collects coverage" --> DinD
    TB -- "reads Crash" --> Redis
    TB -- "writes TracedCrash" --> Redis
    TB -- "reproduces via Docker" --> DinD

    PM -- "reads IndexRequest" --> Redis
    PM -- "writes IndexOutput" --> Redis

    SG -- "generates seeds" --> NodeLocal
    SG -- "writes Crash (PoVs)" --> Redis
    SG -- "queries code" --> PM

    PA -- "reads ConfirmedVuln" --> Redis
    PA -- "writes Patch" --> Redis
    PA -- "queries code" --> PM
    PA -- "builds/tests patches" --> DinD

    PA -- "LLM calls" --> LiteLLM
    SG -- "LLM calls" --> LiteLLM
    LiteLLM --> LLMProviders

    BB --> TaskVol
    FB --> NodeLocal
    CB --> NodeLocal
    TB --> NodeLocal
    PA --> ScratchVol
    DL --> TaskVol
```

## Data Flow

This diagram traces the lifecycle of a vulnerability challenge from initial download through patching and submission.

```mermaid
flowchart TD
    A["Competition API sends task"] --> B["Task Server receives task"]
    B --> C["Task Downloader fetches source repo"]
    C --> D["Scheduler creates IndexRequest + BuildRequests"]

    D --> E["Program Model indexes source code<br/>(CodeQuery + Tree-sitter)"]
    D --> F["Build Bot compiles fuzzing harnesses<br/>(libfuzzer, multiple sanitizers)"]

    E --> G["IndexOutput returned to Scheduler"]
    F --> H["BuildOutput returned to Scheduler"]

    H --> I["Fuzzer Bot runs fuzzing campaign"]
    G --> J["Seed Gen creates targeted inputs"]
    J --> I

    I --> K{"Crash found?"}
    K -- "No" --> I
    K -- "Yes" --> L["Tracer Bot reproduces & traces crash"]

    L --> M["TracedCrash returned to Scheduler"]
    M --> N["Scheduler confirms vulnerability"]
    N --> O["ConfirmedVulnerability sent to Patcher"]

    O --> P["Patcher multi-agent workflow:<br/>1. Input Processing<br/>2. Context Retrieval<br/>3. Root Cause Analysis<br/>4. Patch Strategy<br/>5. Code Generation<br/>6. Build & Test<br/>7. Reflection (retry loop)"]

    P --> Q{"Patch valid?"}
    Q -- "No" --> P
    Q -- "Yes" --> R["Patch returned to Scheduler"]

    R --> S["Scheduler requests patch build verification"]
    S --> T["Build Bot builds patched code"]
    T --> U{"Build + tests pass?"}
    U -- "No" --> V["Scheduler retries or discards"]
    U -- "Yes" --> W["Scheduler submits bundle<br/>to Competition API"]
```

## Redis Queue Topology

All inter-service communication flows through Redis Streams with consumer groups for reliable, at-least-once delivery. Each queue carries protobuf-serialized messages.

```mermaid
flowchart LR
    subgraph "Producers"
        TS_P["Task Server"]
        SCH_P["Scheduler"]
        BB_P["Build Bot"]
        FB_P["Fuzzer Bot"]
        TB_P["Tracer Bot"]
        PM_P["Program Model"]
        PA_P["Patcher"]
        SG_P["Seed Gen"]
    end

    subgraph "Queues"
        Q_DL["orchestrator_download_tasks_queue<br/><i>TaskDownload</i>"]
        Q_DEL["orchestrator_delete_task_queue<br/><i>TaskDelete</i>"]
        Q_RDY["tasks_ready_queue<br/><i>TaskReady</i>"]
        Q_BLD["fuzzer_build_queue<br/><i>BuildRequest</i>"]
        Q_BLO["fuzzer_build_output_queue<br/><i>BuildOutput</i>"]
        Q_CRS["fuzzer_crash_queue<br/><i>Crash</i>"]
        Q_TRC["traced_vulnerabilities_queue<br/><i>TracedCrash</i>"]
        Q_CNF["confirmed_vulnerabilities_queue<br/><i>ConfirmedVulnerability</i>"]
        Q_PAT["patches_queue<br/><i>Patch</i>"]
        Q_IDX["index_queue<br/><i>IndexRequest</i>"]
        Q_IDO["index_output_queue<br/><i>IndexOutput</i>"]
        Q_PRQ["pov_reproducer_requests_queue<br/><i>POVReproduceRequest</i>"]
        Q_PRS["pov_reproducer_responses_queue<br/><i>POVReproduceResponse</i>"]
    end

    subgraph "Consumer Groups"
        CG_ORCH["orchestrator_group"]
        CG_BB["build_bot_consumers"]
        CG_PA["patcher_group"]
        CG_IDX["index_group"]
        CG_TB["tracer_bot_group"]
    end

    %% Producers to Queues
    TS_P --> Q_DL
    TS_P --> Q_DEL
    SCH_P --> Q_BLD
    SCH_P --> Q_IDX
    SCH_P --> Q_CNF
    BB_P --> Q_BLO
    FB_P --> Q_CRS
    TB_P --> Q_TRC
    PM_P --> Q_IDO
    PA_P --> Q_PAT
    SG_P --> Q_CRS

    %% Queues to Consumer Groups
    Q_DL --> CG_ORCH
    Q_DEL --> CG_ORCH
    Q_RDY --> CG_ORCH
    Q_BLO --> CG_ORCH
    Q_TRC --> CG_ORCH
    Q_PAT --> CG_ORCH
    Q_IDO --> CG_ORCH
    Q_PRQ --> CG_ORCH
    Q_PRS --> CG_ORCH
    Q_BLD --> CG_BB
    Q_CRS --> CG_TB
    Q_CNF --> CG_PA
    Q_IDX --> CG_IDX
```

### Queue Summary Table

| Queue | Message Type | Producer(s) | Consumer Group |
|-------|-------------|-------------|----------------|
| `orchestrator_download_tasks_queue` | TaskDownload | Task Server | orchestrator_group |
| `orchestrator_delete_task_queue` | TaskDelete | Task Server | orchestrator_group |
| `tasks_ready_queue` | TaskReady | Downloader | orchestrator_group |
| `fuzzer_build_queue` | BuildRequest | Scheduler | build_bot_consumers |
| `fuzzer_build_output_queue` | BuildOutput | Build Bot | orchestrator_group |
| `fuzzer_crash_queue` | Crash | Fuzzer Bot, Seed Gen | tracer_bot_group |
| `traced_vulnerabilities_queue` | TracedCrash | Tracer Bot | orchestrator_group |
| `confirmed_vulnerabilities_queue` | ConfirmedVulnerability | Scheduler | patcher_group |
| `patches_queue` | Patch | Patcher | orchestrator_group |
| `index_queue` | IndexRequest | Scheduler | index_group |
| `index_output_queue` | IndexOutput | Program Model | orchestrator_group |
| `pov_reproducer_requests_queue` | POVReproduceRequest | Scheduler | orchestrator_group |
| `pov_reproducer_responses_queue` | POVReproduceResponse | Fuzzer Bot | orchestrator_group |

## Deployment Topology

All services run as pods in the `crs` Kubernetes namespace. Shared volumes provide cross-pod access to task data and scratch space.

```mermaid
graph TB
    subgraph "Kubernetes Cluster (namespace: crs)"
        subgraph "API Layer"
            TS_D["task-server<br/><i>Pod</i>"]
            CA_D["competition-api<br/><i>Pod (test mock)</i>"]
        end

        subgraph "Orchestration"
            SCH_D["scheduler<br/><i>Pod</i>"]
            DL_D["task-downloader<br/><i>Pod</i>"]
        end

        subgraph "Fuzzing (4 Build Bot replicas)"
            BB_D["build-bot<br/><i>Pod x4</i>"]
            FB_D["fuzzer-bot<br/><i>Pod</i>"]
            CB_D["coverage-bot<br/><i>Pod</i>"]
            TB_D["tracer-bot<br/><i>Pod</i>"]
        end

        subgraph "Analysis & Patching"
            PM_D["program-model<br/><i>Pod</i>"]
            SG_D["seed-gen<br/><i>Pod</i>"]
            PA_D["patcher<br/><i>Pod</i>"]
        end

        subgraph "Infrastructure Services"
            Redis_D["redis<br/><i>Pod (standalone)</i>"]
            LiteLLM_D["litellm<br/><i>Pod</i>"]
            PG_D["litellm-postgresql<br/><i>Pod</i>"]
            DinD_D["dind-daemon<br/><i>Pod (privileged)</i>"]
            RC_D["registry-cache<br/><i>Pod</i>"]
            SC_D["scratch-cleaner<br/><i>Pod</i>"]
        end

        subgraph "Persistent Volumes"
            PVC_Tasks["tasks_storage PVC<br/>5Gi RWX"]
            PVC_Scratch["crs_scratch PVC<br/>10Gi RWX"]
            PVC_UI["ui_db PVC<br/>1Gi RWO"]
            PVC_Redis["redis PVC<br/>8Gi"]
            PVC_Registry["registry-cache PVC<br/>10Gi"]
            HP_Node["node_data_storage<br/>(hostPath)"]
        end
    end

    %% Volume mounts
    DL_D -. "mount" .-> PVC_Tasks
    BB_D -. "mount" .-> PVC_Tasks
    PA_D -. "mount" .-> PVC_Scratch
    SC_D -. "mount" .-> PVC_Scratch
    Redis_D -. "mount" .-> PVC_Redis
    RC_D -. "mount" .-> PVC_Registry
    TS_D -. "mount" .-> PVC_UI

    FB_D -. "mount" .-> HP_Node
    CB_D -. "mount" .-> HP_Node
    TB_D -. "mount" .-> HP_Node
    SG_D -. "mount" .-> HP_Node
    BB_D -. "mount" .-> HP_Node

    %% Key connections
    LiteLLM_D --> PG_D
    BB_D --> DinD_D
    FB_D --> DinD_D
    CB_D --> DinD_D
    TB_D --> DinD_D
    PA_D --> DinD_D
```

### Container Images

| Service | Image |
|---------|-------|
| Orchestrator (task-server, downloader, scheduler) | `ghcr.io/trailofbits/buttercup/buttercup-orchestrator` |
| Fuzzer (build-bot, fuzzer-bot, coverage-bot, tracer-bot) | `ghcr.io/trailofbits/buttercup/buttercup-fuzzer` |
| Patcher | `ghcr.io/trailofbits/buttercup/buttercup-patcher` |
| Seed Gen | `ghcr.io/trailofbits/buttercup/buttercup-seed-gen` |
| Program Model | `ghcr.io/trailofbits/buttercup/buttercup-program-model` |
| Competition API (test) | `ghcr.io/tob-challenges/example-crs-architecture/competition-test-api` |
