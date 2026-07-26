# Decisions - Loop/Monaco/SSE Plan

## Architecture Decisions
- SSE via FastAPI EventSourceResponse for real-time execution status
- Threading+asyncio execution model (GIL acceptable for I/O-bound test scripts)
- YAML DSL nested structure for loop containers
- X6 embedded sub-graph for loop container visualization
- GitPython for script versioning
- Monaco Editor for Python script editing
