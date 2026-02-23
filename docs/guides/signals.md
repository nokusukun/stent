# External Signals

Signals allow external systems to communicate with running workflows, enabling human-in-the-loop patterns, event-driven workflows, and coordination between systems.

## Overview

A signal is a named message sent to a running execution. Workflows can wait for signals, and external code can send signals to trigger workflow continuation.

```
┌─────────────┐         ┌─────────────┐         ┌─────────────┐
│   Workflow  │         │   Signal    │         │  External   │
│             │         │   Storage   │         │   System    │
└──────┬──────┘         └──────┬──────┘         └──────┬──────┘
       │                       │                       │
       │ wait_for_signal("x")  │                       │
       ├──────────────────────>│                       │
       │                       │                       │
       │    (workflow suspends)│                       │
       │                       │                       │
       │                       │    send_signal("x")   │
       │                       │<──────────────────────┤
       │                       │                       │
       │   signal received     │                       │
       │<──────────────────────┤                       │
       │                       │                       │
       │   (workflow resumes)  │                       │
       │                       │                       │
```

## Waiting for Signals

Use `Stent.wait_for_signal()` in a workflow to pause and wait for an external signal:

```python
from stent import Stent, Result

@Stent.durable()
async def approval_workflow(request_id: str) -> Result[dict, str]:
    # Send notification that approval is needed
    await notify_approvers(request_id)
    
    # Wait for approval signal (this could be hours or days)
    approval = await Stent.wait_for_signal("approval")
    
    if approval["approved"]:
        await process_approved_request(request_id)
        return Result.Ok({
            "status": "approved",
            "approved_by": approval["approver"],
            "approved_at": approval["timestamp"]
        })
    else:
        await notify_rejection(request_id, approval["reason"])
        return Result.Error(f"Rejected: {approval['reason']}")
```

## Sending Signals

External code sends signals using `executor.send_signal()`:

```python
# From your API endpoint, webhook handler, or event processor
await executor.send_signal(
    execution_id="execution-uuid-here",
    name="approval",
    payload={
        "approved": True,
        "approver": "admin@example.com",
        "timestamp": datetime.now().isoformat()
    }
)
```

### Example API Endpoint

```python
from fastapi import FastAPI, HTTPException
from stent import Stent

app = FastAPI()

@app.post("/approvals/{execution_id}")
async def approve_request(
    execution_id: str,
    approved: bool,
    approver: str,
    reason: str = None
):
    try:
        await executor.send_signal(
            execution_id,
            "approval",
            {
                "approved": approved,
                "approver": approver,
                "reason": reason,
                "timestamp": datetime.now().isoformat()
            }
        )
        return {"status": "signal_sent"}
    except ValueError as e:
        raise HTTPException(404, str(e))
```

## Signal Behavior

### Signal Buffering

Signals can be sent before the workflow starts waiting:

```python
@Stent.durable()
async def workflow_with_delay():
    # Do some work first
    await long_running_task()  # Takes 5 minutes
    
    # Now wait for signal
    # If signal was sent during long_running_task, it's buffered
    signal = await Stent.wait_for_signal("trigger")
    
    await process_trigger(signal)
```

The signal is stored in the database and delivered when the workflow calls `wait_for_signal()`.

### Signal Names

Signal names are strings that identify the signal type:

```python
# Different signals for different events
await Stent.wait_for_signal("approval")
await Stent.wait_for_signal("payment_received")
await Stent.wait_for_signal("document_uploaded")
await Stent.wait_for_signal("user_action")
```

### Signal Payload

The payload can be any JSON-serializable data:

```python
# Simple payload
await executor.send_signal(exec_id, "trigger", {"action": "start"})

# Complex payload
await executor.send_signal(exec_id, "form_submitted", {
    "form_id": "ABC123",
    "fields": {
        "name": "John Doe",
        "email": "john@example.com",
        "comments": "Please process urgently"
    },
    "submitted_at": "2024-01-15T10:30:00Z",
    "metadata": {
        "ip_address": "192.168.1.1",
        "user_agent": "Mozilla/5.0..."
    }
})
```

## Common Patterns

### Human Approval Workflow

```python
@Stent.durable()
async def purchase_approval(purchase: dict) -> Result[dict, str]:
    # Determine required approval level
    if purchase["amount"] > 10000:
        approvers = ["cfo@company.com", "ceo@company.com"]
    else:
        approvers = ["manager@company.com"]
    
    # Request approval
    request_id = await create_approval_request(purchase, approvers)
    await send_approval_emails(request_id, approvers)
    
    # Wait for approval
    response = await Stent.wait_for_signal("approval_response")
    
    if response["approved"]:
        order_id = await create_purchase_order(purchase)
        return Result.Ok({"order_id": order_id, "approved_by": response["approver"]})
    else:
        return Result.Error(f"Rejected by {response['approver']}: {response['reason']}")
```

### Multi-Step Approval

```python
@Stent.durable()
async def multi_step_approval(document: dict) -> Result[str, str]:
    stages = ["legal_review", "finance_review", "executive_approval"]
    
    for stage in stages:
        await notify_stage_reviewers(document["id"], stage)
        
        response = await Stent.wait_for_signal(f"{stage}_complete")
        
        if not response["approved"]:
            return Result.Error(f"Rejected at {stage}: {response['reason']}")
        
        await log_approval_stage(document["id"], stage, response)
    
    return Result.Ok(f"Document {document['id']} fully approved")
```

### Event-Driven Processing

```python
@Stent.durable()
async def order_fulfillment(order_id: str) -> dict:
    # Wait for payment confirmation from payment gateway
    payment = await Stent.wait_for_signal("payment_confirmed")
    
    if not payment["success"]:
        await cancel_order(order_id)
        return {"status": "cancelled", "reason": "payment_failed"}
    
    # Start fulfillment
    await start_fulfillment(order_id)
    
    # Wait for shipping confirmation
    shipping = await Stent.wait_for_signal("shipped")
    
    await notify_customer(order_id, shipping["tracking_number"])
    
    # Wait for delivery confirmation
    delivery = await Stent.wait_for_signal("delivered")
    
    return {
        "status": "completed",
        "delivered_at": delivery["timestamp"]
    }
```

### Timeout with Signal

Combine signals with timeouts:

```python
import asyncio

@Stent.durable()
async def approval_with_timeout(request_id: str) -> Result[dict, str]:
    await notify_approvers(request_id)
    
    # Create tasks for signal and timeout
    async def wait_for_approval():
        return await Stent.wait_for_signal("approval")
    
    async def timeout_after(hours: int):
        await Stent.sleep(f"{hours}h")
        return {"timed_out": True}
    
    # Wait for either
    done, pending = await asyncio.wait(
        [
            asyncio.create_task(wait_for_approval()),
            asyncio.create_task(timeout_after(48))
        ],
        return_when=asyncio.FIRST_COMPLETED
    )
    
    for task in pending:
        task.cancel()
    
    result = done.pop().result()
    
    if result.get("timed_out"):
        await escalate_approval(request_id)
        return Result.Error("Approval timed out after 48 hours")
    
    return Result.Ok(result)
```

### Webhook Integration

```python
# Workflow that processes webhook events
@Stent.durable()
async def webhook_processor(webhook_config: dict) -> dict:
    results = []
    
    while True:
        # Wait for next webhook event
        event = await Stent.wait_for_signal("webhook_event")
        
        if event.get("type") == "terminate":
            break
        
        result = await process_webhook(event)
        results.append(result)
        
        # Continue listening for more events
    
    return {"processed": len(results), "results": results}

# Webhook endpoint sends signals
@app.post("/webhooks/{execution_id}")
async def handle_webhook(execution_id: str, event: dict):
    await executor.send_signal(execution_id, "webhook_event", event)
    return {"received": True}
```

## Best Practices

### 1. Use Descriptive Signal Names

```python
# Good: Clear, specific names
await Stent.wait_for_signal("manager_approval")
await Stent.wait_for_signal("payment_gateway_response")
await Stent.wait_for_signal("document_signature_complete")

# Avoid: Generic names
await Stent.wait_for_signal("event")
await Stent.wait_for_signal("signal")
await Stent.wait_for_signal("data")
```

### 2. Validate Signal Payloads

```python
@Stent.durable()
async def workflow_with_validation():
    signal = await Stent.wait_for_signal("user_input")
    
    # Validate required fields
    if "user_id" not in signal:
        raise ValueError("Signal missing required field: user_id")
    
    if signal.get("action") not in ["approve", "reject"]:
        raise ValueError(f"Invalid action: {signal.get('action')}")
    
    # Proceed with validated data
    ...
```

### 3. Include Metadata

```python
# When sending signals, include useful metadata
await executor.send_signal(
    exec_id,
    "approval",
    {
        "approved": True,
        "approver": user.email,
        "timestamp": datetime.now().isoformat(),
        "ip_address": request.client.host,
        "source": "web_ui",
        "comments": user_comments
    }
)
```

### 4. Handle Missing Executions

```python
@app.post("/signals/{execution_id}")
async def send_signal_endpoint(execution_id: str, signal: SignalRequest):
    # Check execution exists
    try:
        state = await executor.state_of(execution_id)
    except ValueError:
        raise HTTPException(404, "Execution not found")
    
    # Check execution is still running
    if state.state in ("completed", "failed", "cancelled"):
        raise HTTPException(400, f"Execution already {state.state}")
    
    await executor.send_signal(execution_id, signal.name, signal.payload)
    return {"sent": True}
```

### 5. Log Signal Activity

```python
@Stent.durable()
async def audited_workflow(request_id: str):
    await log_audit_event(request_id, "waiting_for_approval")
    
    approval = await Stent.wait_for_signal("approval")
    
    await log_audit_event(
        request_id,
        "approval_received",
        {
            "approved": approval["approved"],
            "by": approval["approver"]
        }
    )
    
    ...
```

## Limitations

1. **One consumer per signal**: Only the first `wait_for_signal` call for a given name in an execution will receive the signal.

2. **No signal broadcasting**: A signal is consumed by one waiter; it's not broadcast to multiple waiters.

3. **Execution-scoped**: Signals are specific to an execution; you can't send signals to a function by name.

4. **JSON-serializable payloads**: Signal payloads must be JSON-serializable (or pickle-serializable if using pickle).
