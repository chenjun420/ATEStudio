# Draft: Loop Containers + Monaco + Real-time Status

## Requirements (confirmed)
- **Loop Container Nodes**: Group steps into repeatable loops in the sequence visual editor
- **Monaco Editor Integration**: Script editing with syntax highlighting for Python test scripts
- **Real-time Execution Status**: Live test execution monitoring; communication technology TBD (need evaluation)
- **Execution Model Assessment**: Evaluate serial/parallel/mutex/sub-flow support before designing new features

## Technical Decisions
- Real-time comm technology: PENDING evaluation (WebSocket vs SSE vs NATS WebSocket vs others)
- Loop container design depends on: execution model assessment (can executor run parallel iterations?)

## Research Findings
- (pending explore/librarian agent results)

## Open Questions
- Q1: What communication technology for real-time status? (under evaluation by librarian)
- Q2: Does the executor support parallel execution? (under evaluation by explore)
- Q3: Does the resource manager support mutex/locking? (under evaluation by explore)
- Q4: Can sequences call sub-sequences? (under evaluation by explore)
- Q5: Where should Monaco editor live in the UI? (Settings? PropertyPanel? Dedicated view?)
- Q6: Loop container - visual design: embedded sub-graph vs collapsed node vs tab?
- Q7: Loop container - what loop types? (count-based, condition-based, while, for-each?)
- Q8: Should loop iterations run serially, in parallel, or user-configurable?

## Scope Boundaries
- INCLUDE: Loop container nodes, Monaco editor, real-time execution status, execution model gaps
- EXCLUDE: AI-assisted generation, Qdrant, deployment/CI/CD, frontend testing infrastructure
