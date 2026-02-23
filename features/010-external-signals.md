# External Signals

## Description
External Signals allow a running workflow (orchestrator) to pause execution and wait for an external event. This is useful for "Human-in-the-Loop" scenarios (e.g., waiting for manual approval), webhooks (e.g., waiting for payment confirmation), or inter-workflow synchronization.

Signals are persistent. If a signal is sent before the workflow is ready to receive it, it is buffered and consumed immediately when the workflow calls `wait_for_signal`.

## Key Changes
*   **`Stent.wait_for_signal(name: str)`**: New method to pause workflow execution until a signal with the given name is received.
*   **`executor.send_signal(execution_id: str, name: str, payload: Any)`**: New method to send a signal to a specific execution.
*   **Backend Storage**: A new `signals` table is added to store buffered signals.

## Usage/Configuration

**1. The Workflow (Waiting)**

```python
@Stent.durable()
async def approval_workflow(user_id: str):
    # Do some work...
    await notify_user(user_id)
    
    # Wait for approval signal
    # Workflow suspends here and releases worker resources
    decision = await Stent.wait_for_signal("approval")
    
    if decision.get("approved"):
        await process_request()
    else:
        await reject_request()
```

**2. The External Trigger (Signaling)**

```python
# Called by your API handler (e.g., FastAPI, Flask)
@app.post("/approve/{execution_id}")
async def approve_request(execution_id: str):
    # Send the signal to resume the workflow
    await executor.send_signal(execution_id, "approval", {"approved": True})
    return {"status": "ok"}
```
