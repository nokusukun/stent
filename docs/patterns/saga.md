# Saga Pattern

The Saga pattern manages distributed transactions by breaking them into a sequence of local transactions, with compensating actions to undo completed steps if a later step fails.

## When to Use Sagas

Use the Saga pattern when:

- Multiple services/resources must be coordinated
- All-or-nothing semantics are needed across distributed systems
- Traditional database transactions don't span all resources
- You need to maintain consistency without distributed locks

Common use cases:

- Order processing (inventory, payment, shipping)
- Travel booking (flights, hotels, cars)
- Account provisioning (multiple services)
- Financial transfers (debit, credit, notification)

## Basic Structure

A saga consists of:

1. **Forward actions** - Steps that perform the business logic
2. **Compensating actions** - Steps that undo forward actions on failure
3. **Compensation stack** - Tracks which compensations to run

```python
from stent import Stent, Result

@Stent.durable()
async def saga_workflow(data: dict) -> Result[dict, str]:
    compensations = []
    
    try:
        # Step 1
        result1 = await step1(data)
        compensations.append(lambda: undo_step1(result1))
        
        # Step 2
        result2 = await step2(result1)
        compensations.append(lambda: undo_step2(result2))
        
        # Step 3 (final - no compensation needed)
        result3 = await step3(result2)
        
        return Result.Ok(result3)
        
    except Exception as e:
        # Run compensations in reverse order
        for compensate in reversed(compensations):
            try:
                await compensate()
            except Exception as comp_error:
                # Log but continue with other compensations
                await log_compensation_failure(comp_error)
        
        return Result.Error(f"Saga failed: {e}")
```

## Complete Example: Trip Booking

This example books a trip with flight, hotel, and car rental. If any step fails, previous bookings are cancelled.

### Define Activities

```python
from stent import Stent, Result
import logging

logger = logging.getLogger(__name__)

# Forward actions
@Stent.durable()
async def book_flight(trip_id: str, destination: str) -> str:
    """Book a flight and return confirmation number."""
    # Call flight booking API
    confirmation = await flight_api.book(destination)
    logger.info(f"Flight booked for {trip_id}: {confirmation}")
    return confirmation

@Stent.durable()
async def book_hotel(trip_id: str, destination: str, nights: int) -> str:
    """Book a hotel and return reservation ID."""
    reservation = await hotel_api.reserve(destination, nights)
    logger.info(f"Hotel booked for {trip_id}: {reservation}")
    return reservation

@Stent.durable()
async def book_car(trip_id: str, destination: str, days: int) -> str:
    """Book a rental car and return rental ID."""
    rental = await car_api.rent(destination, days)
    logger.info(f"Car booked for {trip_id}: {rental}")
    return rental

# Compensating actions
@Stent.durable()
async def cancel_flight(trip_id: str, confirmation: str) -> bool:
    """Cancel a flight booking."""
    await flight_api.cancel(confirmation)
    logger.warning(f"Flight cancelled for {trip_id}: {confirmation}")
    return True

@Stent.durable()
async def cancel_hotel(trip_id: str, reservation: str) -> bool:
    """Cancel a hotel reservation."""
    await hotel_api.cancel(reservation)
    logger.warning(f"Hotel cancelled for {trip_id}: {reservation}")
    return True

@Stent.durable()
async def cancel_car(trip_id: str, rental: str) -> bool:
    """Cancel a car rental."""
    await car_api.cancel(rental)
    logger.warning(f"Car cancelled for {trip_id}: {rental}")
    return True
```

### Define the Saga Orchestrator

```python
@Stent.durable()
async def trip_booking_saga(
    trip_id: str,
    destination: str,
    nights: int,
    car_days: int
) -> Result[dict, str]:
    """
    Book a complete trip with flight, hotel, and car.
    If any step fails, all previous bookings are cancelled.
    """
    compensations = []
    bookings = {}
    
    try:
        # Step 1: Book flight
        flight_conf = await book_flight(trip_id, destination)
        bookings["flight"] = flight_conf
        compensations.append(lambda: cancel_flight(trip_id, flight_conf))
        
        # Step 2: Book hotel
        hotel_res = await book_hotel(trip_id, destination, nights)
        bookings["hotel"] = hotel_res
        compensations.append(lambda: cancel_hotel(trip_id, hotel_res))
        
        # Step 3: Book car (final step)
        car_rental = await book_car(trip_id, destination, car_days)
        bookings["car"] = car_rental
        # No compensation for final step - if it succeeds, we're done
        # If it fails, we compensate previous steps
        
        return Result.Ok({
            "trip_id": trip_id,
            "status": "confirmed",
            "bookings": bookings
        })
        
    except Exception as e:
        logger.error(f"Trip booking failed: {e}. Starting compensation...")
        
        # Execute compensations in reverse order
        compensation_errors = []
        for compensate in reversed(compensations):
            try:
                await compensate()
            except Exception as comp_error:
                logger.error(f"Compensation failed: {comp_error}")
                compensation_errors.append(str(comp_error))
        
        error_msg = f"Trip booking failed: {e}"
        if compensation_errors:
            error_msg += f" (compensation errors: {compensation_errors})"
            
        return Result.Error(error_msg)
```

### Using the Saga

```python
async def main():
    backend = Stent.backends.SQLiteBackend("trips.sqlite")
    await backend.init_db()
    
    executor = Stent(backend=backend)
    
    # Start worker
    import asyncio
    worker_task = asyncio.create_task(executor.serve())
    
    # Book a trip
    exec_id = await executor.dispatch(
        trip_booking_saga,
        trip_id="trip_123",
        destination="Hawaii",
        nights=5,
        car_days=7
    )
    
    # Wait for result
    result = await executor.wait_for(exec_id)
    
    if result.ok:
        print(f"Trip booked successfully: {result.value}")
    else:
        print(f"Trip booking failed: {result.error}")
    
    worker_task.cancel()
```

## Handling Compensation Failures

Compensations can fail too. Here are strategies to handle this:

### 1. Log and Continue

Best for non-critical compensations:

```python
for compensate in reversed(compensations):
    try:
        await compensate()
    except Exception as e:
        logger.error(f"Compensation failed: {e}")
        # Continue with other compensations
```

### 2. Retry Compensations

For critical compensations that must succeed:

```python
from stent import RetryPolicy

@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=10,
        initial_delay=1.0,
        backoff_factor=2.0,
        retry_for=(ConnectionError, TimeoutError)
    )
)
async def cancel_payment(payment_id: str) -> bool:
    """Must succeed to avoid double charging."""
    await payment_api.refund(payment_id)
    return True
```

### 3. Dead Letter Queue for Manual Resolution

```python
@Stent.durable()
async def saga_with_dlq_awareness(data: dict) -> Result[dict, str]:
    compensations = []
    
    try:
        # ... saga steps ...
        pass
    except Exception as e:
        failed_compensations = []
        
        for i, compensate in enumerate(reversed(compensations)):
            try:
                await compensate()
            except Exception as comp_error:
                failed_compensations.append({
                    "step": i,
                    "error": str(comp_error)
                })
        
        if failed_compensations:
            # Store for manual resolution
            await store_failed_compensations(data, failed_compensations)
            return Result.Error(f"Saga failed with compensation errors: {failed_compensations}")
        
        return Result.Error(f"Saga failed: {e}")
```

## Saga Variations

### Choreography vs Orchestration

**Orchestration** (shown above): A central orchestrator coordinates all steps.

**Choreography**: Each service publishes events, others react. Less common with Stent since orchestration is its strength.

### Parallel Saga Steps

When steps are independent, run them in parallel:

```python
@Stent.durable()
async def parallel_saga(order: dict) -> Result[dict, str]:
    compensations = []
    
    try:
        # Parallel independent operations
        import asyncio
        
        # These don't depend on each other
        results = await asyncio.gather(
            reserve_inventory(order["items"]),
            validate_shipping_address(order["address"]),
            check_customer_credit(order["customer_id"]),
            return_exceptions=True
        )
        
        # Check for failures
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                raise result
        
        inventory_id, address_valid, credit_ok = results
        
        # Add compensations
        compensations.append(lambda: release_inventory(inventory_id))
        
        # Sequential dependent steps
        payment = await charge_customer(order["customer_id"], order["total"])
        compensations.append(lambda: refund_payment(payment["id"]))
        
        shipment = await create_shipment(order, inventory_id)
        
        return Result.Ok({"order_id": order["id"], "shipment": shipment})
        
    except Exception as e:
        for compensate in reversed(compensations):
            try:
                await compensate()
            except Exception:
                pass
        return Result.Error(str(e))
```

### Nested Sagas

Sagas can call other sagas:

```python
@Stent.durable()
async def create_account_saga(user: dict) -> Result[dict, str]:
    compensations = []
    
    try:
        # Create user record
        user_id = await create_user(user)
        compensations.append(lambda: delete_user(user_id))
        
        # Sub-saga: Set up billing
        billing_result = await billing_setup_saga(user_id)
        if not billing_result.ok:
            raise Exception(f"Billing setup failed: {billing_result.error}")
        # The sub-saga handles its own compensation internally
        compensations.append(lambda: billing_teardown_saga(user_id))
        
        # Sub-saga: Set up permissions
        perms_result = await permissions_setup_saga(user_id)
        if not perms_result.ok:
            raise Exception(f"Permissions setup failed: {perms_result.error}")
        compensations.append(lambda: permissions_teardown_saga(user_id))
        
        return Result.Ok({"user_id": user_id})
        
    except Exception as e:
        for compensate in reversed(compensations):
            try:
                await compensate()
            except Exception:
                pass
        return Result.Error(str(e))
```

## Best Practices

### 1. Make Compensations Idempotent

Compensations may run multiple times (due to retries). Ensure they're safe to repeat:

```python
@Stent.durable(idempotent=True)
async def cancel_booking(booking_id: str) -> bool:
    """Idempotent cancellation - safe to retry."""
    booking = await get_booking(booking_id)
    
    if booking is None or booking["status"] == "cancelled":
        # Already cancelled or never existed
        return True
    
    await set_booking_status(booking_id, "cancelled")
    return True
```

### 2. Store Saga State

For debugging and auditing:

```python
@Stent.durable()
async def audited_saga(order: dict) -> Result[dict, str]:
    saga_id = str(uuid.uuid4())
    
    async def log_step(step: str, status: str, data: dict = None):
        await store_saga_event(saga_id, step, status, data)
    
    compensations = []
    
    try:
        await log_step("inventory", "started")
        inventory = await reserve_inventory(order["items"])
        await log_step("inventory", "completed", {"id": inventory["id"]})
        compensations.append(lambda: release_inventory(inventory["id"]))
        
        # ... more steps with logging ...
        
        await log_step("saga", "completed")
        return Result.Ok({"saga_id": saga_id})
        
    except Exception as e:
        await log_step("saga", "failed", {"error": str(e)})
        # ... compensations ...
        return Result.Error(str(e))
```

### 3. Set Appropriate Timeouts

```python
# Saga should complete within reasonable time
exec_id = await executor.dispatch(
    trip_booking_saga,
    trip_id="trip_123",
    destination="Hawaii",
    nights=5,
    car_days=7,
    max_duration="30m"  # 30 minute timeout
)
```

### 4. Handle Partial Success

Sometimes you want to proceed with partial results:

```python
@Stent.durable()
async def flexible_saga(items: list) -> Result[dict, str]:
    succeeded = []
    failed = []
    
    for item in items:
        try:
            result = await process_item(item)
            succeeded.append(result)
        except Exception as e:
            failed.append({"item": item, "error": str(e)})
    
    if not succeeded:
        return Result.Error("All items failed")
    
    return Result.Ok({
        "succeeded": succeeded,
        "failed": failed,
        "partial": len(failed) > 0
    })
```

### 5. Use Result Type Consistently

Return `Result` from sagas for explicit success/failure handling:

```python
result = await executor.wait_for(exec_id)

if result.ok:
    booking = result.value
    print(f"Success: {booking}")
else:
    error = result.error
    print(f"Failed: {error}")
    # Handle failure (notify customer, create support ticket, etc.)
```

## Testing Sagas

```python
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_saga_success():
    """Test happy path."""
    backend = Stent.backends.SQLiteBackend(":memory:")
    await backend.init_db()
    executor = Stent(backend=backend)
    
    # Mock external APIs
    with patch('flight_api.book', return_value="FL123"):
        with patch('hotel_api.reserve', return_value="HT456"):
            with patch('car_api.rent', return_value="CR789"):
                exec_id = await executor.dispatch(
                    trip_booking_saga,
                    trip_id="test_1",
                    destination="Test",
                    nights=1,
                    car_days=1
                )
                
                # Run worker briefly
                import asyncio
                worker = asyncio.create_task(executor.serve(poll_interval=0.1))
                result = await executor.wait_for(exec_id, expiry=5.0)
                worker.cancel()
                
                assert result.ok
                assert result.value["bookings"]["flight"] == "FL123"

@pytest.mark.asyncio
async def test_saga_compensation():
    """Test that compensation runs on failure."""
    backend = Stent.backends.SQLiteBackend(":memory:")
    await backend.init_db()
    executor = Stent(backend=backend)
    
    cancel_flight_called = False
    cancel_hotel_called = False
    
    async def mock_cancel_flight(*args):
        nonlocal cancel_flight_called
        cancel_flight_called = True
    
    async def mock_cancel_hotel(*args):
        nonlocal cancel_hotel_called
        cancel_hotel_called = True
    
    with patch('flight_api.book', return_value="FL123"):
        with patch('hotel_api.reserve', return_value="HT456"):
            with patch('car_api.rent', side_effect=Exception("No cars")):
                with patch('flight_api.cancel', mock_cancel_flight):
                    with patch('hotel_api.cancel', mock_cancel_hotel):
                        exec_id = await executor.dispatch(
                            trip_booking_saga,
                            trip_id="test_fail",
                            destination="Test",
                            nights=1,
                            car_days=1
                        )
                        
                        import asyncio
                        worker = asyncio.create_task(executor.serve(poll_interval=0.1))
                        result = await executor.wait_for(exec_id, expiry=5.0)
                        worker.cancel()
                        
                        assert not result.ok
                        assert "No cars" in result.error
                        assert cancel_flight_called
                        assert cancel_hotel_called
```

## See Also

- [Error Handling](../guides/error-handling.md) - Retry policies and DLQ management
- [Orchestration](../guides/orchestration.md) - Control flow patterns
- [Batch Processing](batch-processing.md) - Processing large datasets
