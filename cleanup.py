import asyncio
import asyncpg
import os

DSN = os.environ.get("STENT_TEST_PG_DSN")

async def main():
    if not DSN:
        print("No DSN provided")
        return
    print(f"Connecting to {DSN.split('@')[-1]}")
    try:
        conn = await asyncpg.connect(DSN)
        tables = ["executions", "execution_progress", "tasks", "dead_tasks", "cache", "idempotency"]
        for table in tables:
            try:
                # check if table exists first
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = $1)",
                    table
                )
                if exists:
                    await conn.execute(f"TRUNCATE TABLE {table} CASCADE")
                    print(f"Truncated {table}")
                else:
                    print(f"Table {table} does not exist")
            except Exception as e:
                print(f"Error truncating {table}: {e}")
        await conn.close()
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
