import sqlite3
import asyncio
from typing import Any, List, Optional

class AsyncCursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    @property
    def rowcount(self):
        return self._cursor.rowcount

    async def fetchone(self) -> Any:
        return await asyncio.to_thread(self._cursor.fetchone)

    async def fetchall(self) -> List[Any]:
        return await asyncio.to_thread(self._cursor.fetchall)
    
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, exc_type, exc, tb):
        pass 

class ExecuteContext:
    def __init__(self, conn, sql, parameters):
        self.conn = conn
        self.sql = sql
        self.parameters = parameters
        self.cursor = None

    def __await__(self):
        return self._execute().__await__()

    async def _execute(self):
        self.cursor = await self.conn._execute_impl(self.sql, self.parameters)
        return self.cursor

    async def __aenter__(self):
        self.cursor = await self._execute()
        return self.cursor

    async def __aexit__(self, exc_type, exc, tb):
        pass

class AsyncConnection:
    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None

    async def __aenter__(self):
        self._conn = await asyncio.to_thread(sqlite3.connect, self.path, check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._conn:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    def execute(self, sql: str, parameters: tuple = ()) -> ExecuteContext:
        return ExecuteContext(self, sql, parameters)
        
    async def _execute_impl(self, sql, parameters):
        assert self._conn is not None
        cursor = await asyncio.to_thread(self._conn.execute, sql, parameters)
        return AsyncCursor(cursor)

    async def commit(self):
        assert self._conn is not None
        await asyncio.to_thread(self._conn.commit)
        
    @property
    def row_factory(self):
        assert self._conn is not None
        return self._conn.row_factory
    
    @row_factory.setter
    def row_factory(self, val):
        assert self._conn is not None
        self._conn.row_factory = val

def connect(path: str) -> AsyncConnection:
    return AsyncConnection(path)

def register_adapter(type, adapter):
    sqlite3.register_adapter(type, adapter)

def register_converter(typename, converter):
    sqlite3.register_converter(typename, converter)

Row = sqlite3.Row