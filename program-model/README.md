# Buttercup Program Model

## Overview

The Program Model is the semantic code analysis component of the Buttercup CRS. It indexes a program's source code to build a queryable representation that other components (`patcher` and `seed-gen`) use to understand code structure, navigate call graphs, and retrieve relevant code snippets during vulnerability analysis and patch generation.

The component combines [CodeQuery](https://ruben2020.github.io/codequery/) for symbol indexing and cross-reference queries with [Tree-sitter](https://tree-sitter.github.io/tree-sitter/) for precise syntax-aware code parsing. Together, these tools enable function lookup, caller/callee analysis, type definition retrieval, and fuzzy symbol matching across entire codebases.

## Architecture

```
program-model/
  src/buttercup/program_model/
    __cli__.py              # CLI entry point (serve and process subcommands)
    program_model.py        # Main ProgramModel service (queue consumer and task processor)
    codequery.py            # CodeQuery and CodeQueryPersistent classes
    settings.py             # Pydantic-based configuration
    api/
      tree_sitter.py        # Tree-sitter parsing (CodeTS class)
      fuzzy_imports_resolver.py  # Import resolution for C and Java
    utils/
      common.py             # Shared data models (Function, TypeDefinition, etc.)
```

### Key Modules

**`program_model.py`** - The `ProgramModel` dataclass is the top-level service. It consumes `IndexRequest` messages from the `INDEX` Redis queue, processes each task by building a CodeQuery database, and publishes `IndexOutput` messages to the `INDEX_OUTPUT` queue. Supports two modes: `serve` (continuous queue consumer) and `process` (single task).

**`codequery.py`** - Contains two classes:
- `CodeQuery`: Builds and queries a CodeQuery database for a challenge task. Handles building the fuzzing container, copying its `/src` directory, generating cscope/ctags indexes, and creating the `codequery.db`. Provides methods for querying functions, callers, callees, types, and type usages.
- `CodeQueryPersistent`: A subclass that persists the database to a known location based on task ID, allowing reuse across multiple service instances.

**`api/tree_sitter.py`** - The `CodeTS` class uses Tree-sitter to parse source files and extract function definitions and type definitions with precise line-number ranges. It uses language-specific Tree-sitter queries for C, C++, and Java, and includes LRU caching for performance. Also supports extracting class/interface members (fields, methods) for Java.

**`api/fuzzy_imports_resolver.py`** - Contains two resolvers for deduplicating CodeQuery results:
- `FuzzyCImportsResolver`: Resolves C/C++ `#include` dependencies to determine which callee definitions are actually reachable from a given caller, filtering out false positives from CodeQuery's syntactic search.
- `FuzzyJavaImportsResolver`: Resolves Java `import` statements and dot-expression call prefixes to identify which class methods are actually invoked, supporting nested field/method type resolution.

**`utils/common.py`** - Shared data models used across the component: `Function`, `FunctionBody`, `TypeDefinition`, `TypeDefinitionType`, and `TypeUsageInfo`.

## Supported Languages

| Language | Function Extraction | Type Extraction | Import Resolution | File Extensions |
|----------|-------------------|-----------------|-------------------|-----------------|
| C        | Yes               | Yes (struct, union, enum, typedef, preprocessor macros) | Yes (`FuzzyCImportsResolver`) | `.c`, `.h`, `.inc`, `.inl`, `.ipp`, `.tpp`, `.y`, `.l`, `.lex`, `.yacc`, `.in`, `.m`, `.cu`, `.cuh` |
| C++      | Yes               | Yes (class, struct, union, enum, typedef, preprocessor macros) | Yes (`FuzzyCImportsResolver`) | `.cpp`, `.cxx`, `.cc`, `.C`, `.c++`, `.hpp`, `.hxx`, `.hh`, `.H`, `.h++`, `.mm` |
| Java     | Yes               | Yes (class, interface, enum, record, annotation) | Yes (`FuzzyJavaImportsResolver`) | `.java`, `.jsp`, `.jspx`, `.tag`, `.jspf`, `.properties`, `.gradle`, `.kt`, `.scala`, `.groovy`, `.aj` |

## Key Features

### CodeQuery Integration
- Builds a CodeQuery database by compiling fuzzing containers, extracting the `/src` directory, and indexing with `cscope`, `ctags`, and `cqmakedb`
- Queries symbols, functions, classes/structs, callers, callees, and type usages via `cqsearch`
- Persistent database mode (`CodeQueryPersistent`) caches the database per task ID for reuse

### Tree-sitter Parsing
- Language-specific Tree-sitter queries extract function definitions with exact line ranges
- Handles complex patterns: nested declarators, macros (e.g., `PNG_FUNCTION`), qualified C++ names, Java generics
- Preprocessor directive stripping for C/C++ to improve parse accuracy
- LRU caching (1000 entries) on function and file lookups

### Fuzzy Matching
- Fuzzy function and type name matching using `rapidfuzz` with configurable similarity thresholds (default: 80%)
- Results ordered with exact matches first, then fuzzy matches sorted by descending similarity score

### Import-Aware Callee Filtering
- Deduplicates callee search results by tracing `#include` (C/C++) or `import` (Java) dependency chains
- Falls back to returning all candidates when import resolution is inconclusive, to avoid dropping valid results

## Configuration

The component uses Pydantic Settings with the `BUTTERCUP_PROGRAM_MODEL_` environment variable prefix.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BUTTERCUP_PROGRAM_MODEL_SCRATCH_DIR` | Working directory for scratch space | `/tmp/scratch` |
| `BUTTERCUP_PROGRAM_MODEL_LOG_LEVEL` | Log level (`debug`, `info`, `warning`, `error`) | `info` |
| `BUTTERCUP_PROGRAM_MODEL_LOG_MAX_LINE_LENGTH` | Maximum log line length (truncation) | `None` (unlimited) |

### Serve Subcommand Settings

| Variable / CLI Flag | Description | Default |
|---------------------|-------------|---------|
| `--redis_url` | Redis connection URL | `redis://localhost:6379` |
| `--sleep_time` | Seconds between queue polling | `1.0` |
| `--python` | Python interpreter path | `python` |
| `--allow_pull` / `--no-allow_pull` | Allow pulling images during builds | `True` |

### Process Subcommand Settings

| Variable / CLI Flag | Description | Default |
|---------------------|-------------|---------|
| `--task_dir` | Path to the task directory | (required) |
| `--task_id` | Task identifier | (required) |
| `--python` | Python interpreter path | `python` |
| `--allow_pull` / `--no-allow_pull` | Allow pulling images during builds | `True` |

Settings can also be provided via a `.env` file.

## Usage

```bash
# Service mode: continuously consume from Redis INDEX queue
buttercup-program-model serve --redis_url redis://localhost:6379

# Single task mode: index one task directly
buttercup-program-model process --task_dir /path/to/task --task_id my-task-id
```

## Development

### Setup

```bash
cd program-model
uv sync --all-extras
```

### System Dependencies

The following command-line tools must be installed and available on `PATH`:
- `cscope` - C source code cross-reference tool
- `ctags` - Source code tag generator
- `cqmakedb` - CodeQuery database builder
- `cqsearch` - CodeQuery search tool
- `docker` - Required for building fuzzing containers and copying source code

Install CodeQuery from [ruben2020/codequery](https://ruben2020.github.io/codequery/). The project also uses a custom cscope fork: [buttercup-cscope](https://github.com/trail-of-forks/buttercup-cscope).

### Testing

```bash
# Run unit tests (skips integration tests)
cd program-model && uv run pytest

# Run unit tests with verbose output
cd program-model && uv run pytest -svv

# Run integration tests (requires Docker and network access to clone repositories)
cd program-model && uv run pytest -svv --runintegration

# Run a specific integration test
cd program-model && uv run pytest -svv --runintegration tests/c/test_libpng.py

# Keep test artifacts for debugging
cd program-model && uv run pytest -svv --runintegration --no-cleanup
```

Integration tests clone real OSS-Fuzz projects (libpng, libxml2, sqlite, dropbear, freerdp, libjpeg-turbo, hdf5, log4j2, checkstyle, graphql-java, zookeeper, etc.) and build CodeQuery databases to verify function lookup, caller/callee resolution, and type extraction.

### Linting and Type Checking

```bash
# Format, lint, and type-check
cd program-model && make all

# Or individually
cd program-model && make reformat   # ruff format + fix
cd program-model && make lint       # ruff check + mypy
cd program-model && make test       # pytest with integration tests
```

## Integration

### Redis Queues

| Queue | Direction | Message Type | Description |
|-------|-----------|--------------|-------------|
| `INDEX` | Input | `IndexRequest` (protobuf) | Requests to index a task's source code |
| `INDEX_OUTPUT` | Output | `IndexOutput` (protobuf) | Signals that indexing is complete for a task |

The service uses Redis consumer groups (`GroupNames.INDEX`) for reliable message processing with acknowledgment.

### Task Registry

The service checks the `TaskRegistry` before processing each task to skip cancelled or expired tasks.

### Shared Storage

- Reads task source code from the task directory (via `ChallengeTask`)
- Writes the CodeQuery database and container source to the task directory
- Uses `node_local.dir_to_remote_archive()` to push indexed artifacts to shared remote storage for other components to access

### Other Components

- **Orchestrator**: Sends `IndexRequest` messages via the `INDEX` queue after downloading task sources
- **Patcher**: Uses the CodeQuery database (via `CodeQueryPersistent`) to retrieve code snippets, function definitions, and call graphs for vulnerability analysis and patch generation
- **Seed-gen**: Queries the indexed code model for targeted test case generation

### Telemetry

All major operations emit OpenTelemetry spans with CRS-specific attributes:
- `index_task_with_codequery` - Task indexing
- `get_functions_with_codequery` - Function lookups
- `get_callers_with_codequery` - Caller analysis
- `get_callees_with_codequery` - Callee analysis
- `get_types_with_codequery` - Type lookups
- `get_type_calls_with_codequery` - Type usage lookups

## Dependencies

* [CodeQuery](https://ruben2020.github.io/codequery/) - Symbol indexing and cross-reference database
* [buttercup-cscope](https://github.com/trail-of-forks/buttercup-cscope) - Custom cscope fork
* [Tree-sitter](https://tree-sitter.github.io/tree-sitter/) ~0.24.0 - Incremental parsing library
* [tree-sitter-language-pack](https://pypi.org/project/tree-sitter-language-pack/) ~0.9.0 - Pre-built Tree-sitter grammars
* [rapidfuzz](https://pypi.org/project/rapidfuzz/) ~3.12.2 - Fuzzy string matching
* [common](../common/) - Shared Buttercup utilities (Redis queues, protobuf messages, telemetry, challenge tasks)
