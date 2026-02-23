import asyncio
import os
import logging
from stent import Stent, Result, RetryPolicy

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger("SagaExample")

# --- Activities (Steps) ---

@Stent.durable()
async def book_flight(trip_id: str) -> str:
    # Simulate success
    await asyncio.sleep(0.2)
    logger.info(f"Flight booked for {trip_id}")
    return f"flight_{trip_id}"

@Stent.durable()
async def cancel_flight(trip_id: str):
    await asyncio.sleep(0.2)
    logger.warning(f"Flight cancelled for {trip_id}")
    return True

@Stent.durable()
async def book_hotel(trip_id: str) -> str:
    await asyncio.sleep(0.2)
    logger.info(f"Hotel booked for {trip_id}")
    return f"hotel_{trip_id}"

@Stent.durable()
async def cancel_hotel(trip_id: str):
    await asyncio.sleep(0.2)
    logger.warning(f"Hotel cancelled for {trip_id}")
    return True

@Stent.durable()
async def book_car(trip_id: str) -> str:
    await asyncio.sleep(0.2)
    # Simulate failure for a specific trip ID to test compensation
    if "fail_car" in trip_id:
        raise ValueError("No cars available!")
    logger.info(f"Car booked for {trip_id}")
    return f"car_{trip_id}"

@Stent.durable()
async def cancel_car(trip_id: str):
    await asyncio.sleep(0.2)
    logger.warning(f"Car cancelled for {trip_id}")
    return True

# --- Orchestrator (Saga) ---

@Stent.durable()
async def trip_booking_saga(trip_id: str) -> Result[dict, Exception]:
    compensations = []
    data = {}
    
    try:
        # Step 1: Flight
        flight_ref = await book_flight(trip_id)
        data["flight"] = flight_ref
        compensations.append(lambda: cancel_flight(trip_id))
        
        # Step 2: Hotel
        hotel_ref = await book_hotel(trip_id)
        data["hotel"] = hotel_ref
        compensations.append(lambda: cancel_hotel(trip_id))
        
        # Step 3: Car (might fail)
        car_ref = await book_car(trip_id)
        data["car"] = car_ref
        # compensations.append(lambda: cancel_car(trip_id)) # Not needed if last step
        
        return Result.Ok(data)

    except Exception as e:
        logger.error(f"Saga failed: {e}. Starting compensation...")
        # Execute compensations in reverse order
        for comp in reversed(compensations):
            try:
                await comp()
            except Exception as ce:
                logger.error(f"Compensation failed: {ce}")
        
        return Result.Error(e)

# --- Runner ---

async def main():
    db_path = "saga_example.sqlite"
    if os.path.exists(db_path):
        os.remove(db_path)
    
    backend = Stent.backends.SQLiteBackend(db_path)
    await backend.init_db()
    executor = Stent(backend=backend)
    worker_task = asyncio.create_task(executor.serve(poll_interval=0.1))
    
    # 1. Successful Trip
    logger.info("--- Starting Successful Trip ---")
    exec_id_1 = await executor.dispatch(trip_booking_saga, "trip_hawaii")
    await _wait(executor, exec_id_1)
    
    # 2. Failed Trip (Car failure triggers rollbacks)
    logger.info("--- Starting Failed Trip (Saga Rollback) ---")
    exec_id_2 = await executor.dispatch(trip_booking_saga, "trip_fail_car")
    await _wait(executor, exec_id_2)
    
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

async def _wait(executor, exec_id):
    while True:
        state = await executor.state_of(exec_id)
        if state.state in ("completed", "failed", "timed_out"):
            break
        await asyncio.sleep(0.2)
    
    # The saga handles internal errors and returns Result.Error, so state might be 'completed'
    # but result is Error.
    res = await executor.result_of(exec_id)
    if res.ok:
        logger.info(f"Saga Result: SUCCESS - {res.value}")
    else:
        logger.info(f"Saga Result: FAILED (Handled) - {res.error}")

if __name__ == "__main__":
    asyncio.run(main())
