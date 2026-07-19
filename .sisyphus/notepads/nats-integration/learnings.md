# Learnings

## NATS Message Handler Implementation

### JSON Parsing Pattern
- Use `json.loads(msg.data.decode())` to parse NATS message data
- Catch `json.JSONDecodeError` for invalid JSON
- Always decode bytes to string before JSON parsing

### Message Acknowledgment Pattern
- Use `await msg.ack()` for successful processing
- Use `await msg.nak()` for failures (parse errors, exceptions)
- NATS JetStream will redeliver NAK'd messages

### Subject Routing Pattern
- Use `msg.subject.startswith("prefix.")` to route messages
- Keeps handler extensible for multiple message types
- Log unknown subjects at DEBUG level (not ERROR - not critical)

### Type Annotations with NATS
- NATS library has incomplete type annotations
- Use `# type: ignore[misc]` on `_handle_message` parameter
- Accept type warnings from basedpyright for NATS internals

### Logging Pattern
- INFO level: successful processing, received results
- ERROR level: JSON decode errors, handling exceptions
- DEBUG level: unknown subjects (expected scenario)
