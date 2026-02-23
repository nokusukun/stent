# Orchestration Patterns

This guide covers how to compose durable functions into workflows, including orchestrator patterns, sub-workflows, and control flow.

## Orchestrators vs Activities

In Stent, you'll write two types of durable functions:

### Activities

Activities perform actual work - they have side effects and interact with external systems:

```python
@Stent.durable()
async def send_email(to: str, subject: str, body: str) -> bool:
    # Actual side effect: sends an email
    return await email_client.send(to, subject, body)

@Stent.durable()
async def charge_payment(customer_id: str, amount: int) -> str:
    # Actual side effect: charges payment
    return await payment_gateway.charge(customer_id, amount)

@Stent.durable()
async def update_database(record_id: str, data: dict) -> bool:
    # Actual side effect: updates database
    await db.update(record_id, data)
    return True
```

### Orchestrators

Orchestrators coordinate activities and define workflow logic:

```python
@Stent.durable()
async def order_workflow(order: dict) -> Result[dict, Exception]:
    # Step 1: Validate
    validated = await validate_order(order)
    
    # Step 2: Reserve inventory
    reservation = await reserve_inventory(validated["items"])
    
    # Step 3: Charge payment
    payment = await charge_payment(validated["customer_id"], validated["total"])
    
    # Step 4: Confirm order
    confirmation = await confirm_order(order["id"], payment["transaction_id"])
    
    # Step 5: Notify customer
    await send_email(
        validated["customer_email"],
        "Order Confirmed",
        f"Your order {order['id']} is confirmed!"
    )
    
    return Result.Ok(confirmation)
```

## Control Flow

### Sequential Execution

Activities are executed in sequence when you `await` them one after another:

```python
@Stent.durable()
async def sequential_workflow(data: dict) -> dict:
    # Each step waits for the previous to complete
    step1_result = await first_step(data)
    step2_result = await second_step(step1_result)
    step3_result = await third_step(step2_result)
    return step3_result
```

### Conditional Logic

Use standard Python conditionals:

```python
@Stent.durable()
async def conditional_workflow(order: dict) -> Result[str, str]:
    # Check order type
    if order["type"] == "express":
        await process_express_order(order)
        await send_express_notification(order["customer_id"])
    elif order["type"] == "standard":
        await process_standard_order(order)
        await schedule_delivery(order["id"])
    else:
        return Result.Error(f"Unknown order type: {order['type']}")
    
    # Check for add-ons
    if order.get("gift_wrap"):
        await add_gift_wrapping(order["id"])
    
    return Result.Ok("Order processed")
```

### Loops

Use standard Python loops:

```python
@Stent.durable()
async def batch_workflow(items: list[dict]) -> dict:
    results = []
    errors = []
    
    for item in items:
        try:
            result = await process_item(item)
            results.append(result)
        except Exception as e:
            errors.append({"item": item, "error": str(e)})
    
    return {
        "processed": len(results),
        "failed": len(errors),
        "errors": errors
    }
```

### Retry Logic

You can implement custom retry logic in orchestrators:

```python
@Stent.durable()
async def workflow_with_retry(data: dict) -> Result[dict, str]:
    max_retries = 3
    
    for attempt in range(max_retries):
        result = await try_operation(data)
        
        if result["success"]:
            return Result.Ok(result)
        
        if attempt < max_retries - 1:
            # Wait before retrying (durable sleep)
            await Stent.sleep(f"{2 ** attempt}s")  # 1s, 2s, 4s
    
    return Result.Error("Operation failed after retries")
```

## Sub-Workflows

Orchestrators can call other orchestrators:

```python
@Stent.durable()
async def payment_workflow(order_id: str, customer_id: str, amount: int) -> dict:
    # Sub-workflow handles all payment logic
    authorization = await authorize_payment(customer_id, amount)
    capture = await capture_payment(authorization["auth_id"])
    return {"payment_id": capture["id"], "status": "captured"}

@Stent.durable()
async def order_workflow(order: dict) -> Result[dict, Exception]:
    # Validate first
    await validate_order(order)
    
    # Call sub-workflow for payment
    payment = await payment_workflow(
        order["id"],
        order["customer_id"],
        order["total"]
    )
    
    # Continue with fulfillment
    await start_fulfillment(order["id"], payment["payment_id"])
    
    return Result.Ok({"order_id": order["id"], "payment": payment})
```

### Benefits of Sub-Workflows

1. **Modularity**: Break complex workflows into manageable pieces
2. **Reusability**: Use the same sub-workflow in multiple places
3. **Isolation**: Sub-workflow failures can be handled independently
4. **Testing**: Test sub-workflows in isolation

## Waiting Patterns

### Durable Sleep

Use `Stent.sleep()` for delays that don't block workers:

```python
@Stent.durable()
async def scheduled_workflow(notification_id: str) -> None:
    # Send immediate notification
    await send_notification(notification_id, "Starting process")
    
    # Wait 24 hours (worker is free during this time)
    await Stent.sleep("24h")
    
    # Send follow-up
    await send_notification(notification_id, "Process check-in")
    
    # Wait another week
    await Stent.sleep("7d")
    
    # Final notification
    await send_notification(notification_id, "Process complete")
```

### Waiting for External Signals

Use signals for human-in-the-loop or external event workflows:

```python
@Stent.durable()
async def approval_workflow(request: dict) -> Result[dict, str]:
    # Send approval request
    await notify_approvers(request["id"], request["approvers"])
    
    # Wait for approval signal (could be hours or days)
    approval = await Stent.wait_for_signal("approval")
    
    if approval["approved"]:
        await execute_request(request)
        return Result.Ok({"status": "approved", "by": approval["approver"]})
    else:
        await notify_rejection(request["id"], approval["reason"])
        return Result.Error(f"Rejected: {approval['reason']}")
```

External code sends the signal:

```python
# From your API endpoint or event handler
await executor.send_signal(
    execution_id,
    "approval",
    {"approved": True, "approver": "admin@example.com"}
)
```

### Waiting with Timeout

Combine signals with timeouts:

```python
@Stent.durable()
async def approval_with_timeout(request: dict) -> Result[dict, str]:
    await notify_approvers(request["id"], request["approvers"])
    
    # Create a timeout task
    timeout_task = asyncio.create_task(wait_for_timeout(request["id"], hours=48))
    signal_task = asyncio.create_task(Stent.wait_for_signal("approval"))
    
    # Wait for either
    done, pending = await asyncio.wait(
        [timeout_task, signal_task],
        return_when=asyncio.FIRST_COMPLETED
    )
    
    # Cancel the pending task
    for task in pending:
        task.cancel()
    
    # Check which completed
    completed_task = done.pop()
    if completed_task == timeout_task:
        return Result.Error("Approval timed out")
    else:
        approval = completed_task.result()
        return Result.Ok(approval)
```

## Error Handling

### Try/Except in Orchestrators

```python
@Stent.durable()
async def robust_workflow(data: dict) -> Result[dict, str]:
    try:
        result = await risky_operation(data)
        return Result.Ok(result)
    except ValidationError as e:
        # Handle validation errors
        await log_validation_error(data, str(e))
        return Result.Error(f"Validation failed: {e}")
    except NetworkError as e:
        # Handle network errors
        await queue_for_retry(data)
        return Result.Error(f"Network error: {e}")
    except Exception as e:
        # Handle unexpected errors
        await alert_on_call(str(e))
        raise  # Re-raise to trigger retry policy
```

### Compensation (Saga Pattern)

For distributed transactions, implement compensation:

```python
@Stent.durable()
async def booking_saga(trip: dict) -> Result[dict, str]:
    compensations = []
    
    try:
        # Book flight
        flight = await book_flight(trip["flight"])
        compensations.append(lambda: cancel_flight(flight["id"]))
        
        # Book hotel
        hotel = await book_hotel(trip["hotel"])
        compensations.append(lambda: cancel_hotel(hotel["id"]))
        
        # Book car
        car = await book_car(trip["car"])
        # No compensation needed for last step
        
        return Result.Ok({
            "flight": flight,
            "hotel": hotel,
            "car": car
        })
        
    except Exception as e:
        # Execute compensations in reverse order
        for compensate in reversed(compensations):
            try:
                await compensate()
            except Exception as comp_error:
                await log_compensation_failure(comp_error)
        
        return Result.Error(f"Booking failed: {e}")
```

See [Saga Pattern](../patterns/saga.md) for a complete example.

## Determinism Guidelines

Orchestrators should be deterministic - the same inputs should produce the same execution path. This is important because orchestrators may be replayed during recovery.

### Do

```python
@Stent.durable()
async def deterministic_workflow(order: dict) -> dict:
    # Good: Logic depends only on input
    if order["total"] > 1000:
        await apply_large_order_discount(order["id"])
    
    # Good: Get current time from activity
    timestamp = await get_current_timestamp()
    
    # Good: Get random value from activity
    random_id = await generate_random_id()
    
    return {"processed_at": timestamp, "id": random_id}
```

### Don't

```python
@Stent.durable()
async def non_deterministic_workflow(order: dict) -> dict:
    # Bad: Different result on replay
    import random
    if random.random() > 0.5:
        await path_a()
    else:
        await path_b()
    
    # Bad: Different result on replay
    from datetime import datetime
    if datetime.now().hour < 12:
        await morning_process()
    
    # Bad: Reading from global state
    if GLOBAL_CONFIG["feature_flag"]:
        await new_feature()
```

### Wrapping Non-Deterministic Code

```python
# Wrap non-deterministic operations in activities
@Stent.durable()
async def get_random_value() -> float:
    import random
    return random.random()

@Stent.durable()
async def get_current_time() -> str:
    from datetime import datetime
    return datetime.now().isoformat()

# Use in orchestrator
@Stent.durable()
async def workflow() -> dict:
    random_val = await get_random_value()  # Stored, replayed consistently
    timestamp = await get_current_time()   # Stored, replayed consistently
    
    if random_val > 0.5:
        await path_a()
    return {"timestamp": timestamp}
```

## Workflow Patterns Summary

| Pattern | Use Case |
|---------|----------|
| Sequential | Steps must happen in order |
| Conditional | Different paths based on data |
| Loop | Process multiple items |
| Parallel | Independent tasks (see [Parallel Execution](parallel-execution.md)) |
| Sub-workflow | Reusable workflow components |
| Saga | Distributed transactions with rollback |
| Human-in-the-loop | Requires external approval |
| Scheduled | Time-based triggers and delays |
