import asyncio
import pytest
import os
from senpuki import Senpuki, Result

from datetime import timedelta

@Senpuki.durable()
async def signal_workflow():
    val = await Senpuki.wait_for_signal("my_signal")
    return val

@Senpuki.durable()
async def multi_signal_workflow():
    s1 = await Senpuki.wait_for_signal("sig1")
    s2 = await Senpuki.wait_for_signal("sig2")
    return s1 + s2

@pytest.mark.asyncio
async def test_signal_basic():
    db_path = "test_signals.sqlite"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    backend = Senpuki.backends.SQLiteBackend(db_path)
    await backend.init_db()
    executor = Senpuki(backend=backend)
    
    # Start worker
    worker_task = asyncio.create_task(executor.serve(poll_interval=0.1))
    
    try:
        # Dispatch
        exec_id = await executor.dispatch(signal_workflow)
        
        # Wait a bit for it to reach wait state
        await asyncio.sleep(0.5)
        
        state = await executor.state_of(exec_id)
        # It might be running (waiting) or failed if bug
        if state.state == "failed":
            # Print error
            res = await executor.result_of(exec_id)
            print(res)
            
        assert state.state == "running"
        
        # Send signal
        await executor.send_signal(exec_id, "my_signal", "hello")
        
        # Wait for completion
        res = await executor.wait_for(exec_id, expiry=5.0)
        assert res.ok
        assert res.value == "hello"
        
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await backend.close()
        if os.path.exists(db_path):
            os.remove(db_path)

@pytest.mark.asyncio
async def test_signal_buffered():
    """Test sending signal BEFORE workflow starts"""
    db_path = "test_signals_buffered.sqlite"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    backend = Senpuki.backends.SQLiteBackend(db_path)
    await backend.init_db()
    executor = Senpuki(backend=backend)
    worker_task = asyncio.create_task(executor.serve(poll_interval=0.1))
    
    try:
        # Dispatch with delay
        exec_id = await executor.dispatch(signal_workflow, delay=timedelta(seconds=0.5)) 
        
        # Send signal immediately
        await executor.send_signal(exec_id, "my_signal", "early_bird")
        
        # Wait
        res = await executor.wait_for(exec_id, expiry=5.0)
        assert res.ok
        assert res.value == "early_bird"
        
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except: pass
        await backend.close()
        if os.path.exists(db_path):
            os.remove(db_path)

@pytest.mark.asyncio
async def test_multi_signals():
    db_path = "test_multi_signals.sqlite"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    backend = Senpuki.backends.SQLiteBackend(db_path)
    await backend.init_db()
    executor = Senpuki(backend=backend)
    worker_task = asyncio.create_task(executor.serve(poll_interval=0.1))
    
    try:
        exec_id = await executor.dispatch(multi_signal_workflow)
        
        await asyncio.sleep(0.5)
        await executor.send_signal(exec_id, "sig1", 10)
        
        await asyncio.sleep(0.5)
        await executor.send_signal(exec_id, "sig2", 20)
        
        res = await executor.wait_for(exec_id, expiry=5.0)
        assert res.ok
        assert res.value == 30
        
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except: pass
        await backend.close()
        if os.path.exists(db_path):
            os.remove(db_path)
