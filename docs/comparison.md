# Comparison with Other Libraries

How Stent compares to other Python workflow orchestration and task queue libraries.

## Quick Comparison

| Feature | Temporal | Celery | Dramatiq | Prefect | Airflow | Azure DF | Restate | **Stent** |
|---------|----------|--------|----------|---------|---------|----------|---------|-------------|
| **Durable Execution** | Yes | No | No | Partial | No | Yes | Yes | **Yes** |
| **Setup Complexity** | High | Medium | Low | Low-Med | High | Medium | Low | **Very Low** |
| **Infrastructure** | Server cluster | Broker + Backend | Broker | Server | Multi-component | Azure | Single binary | **SQLite/Postgres** |
| **Native Async** | Yes | No | No | Yes | Limited | Generator | Yes | **Yes** |
| **Managed Option** | Yes | No | No | Yes | Yes | Yes | Yes | No |
| **Primary Use Case** | Mission-critical workflows | Task queues | Task queues | Data pipelines | Batch ETL | Serverless | Distributed apps | **App workflows** |

## Detailed Comparisons

### vs Temporal

[Temporal](https://temporal.io) is the most similar in philosophy—true durable execution with automatic replay. However, it requires significant infrastructure.

**Temporal requires:**
- Temporal Server (cluster)
- Database (Cassandra, MySQL, or PostgreSQL)
- Separate worker processes
- Understanding of task queues, namespaces, activities vs workflows

**Stent requires:**
- SQLite file (dev) or PostgreSQL (prod)
- That's it

**Code comparison:**

```python
# Temporal - class-based workflows, separate activity definitions
@workflow.defn
class OrderWorkflow:
    @workflow.run
    async def run(self, order_id: str) -> dict:
        return await workflow.execute_activity(
            process_order,
            order_id,
            start_to_close_timeout=timedelta(seconds=30),
        )

@activity.defn
async def process_order(order_id: str) -> dict:
    return {"order_id": order_id, "status": "done"}
```

```python
# Stent - just decorated functions
@Stent.durable()
async def process_order(order_id: str) -> dict:
    return {"order_id": order_id, "status": "done"}

@Stent.durable()
async def order_workflow(order_id: str) -> dict:
    return await process_order(order_id)
```

**Choose Stent over Temporal when:**
- You need durability but can't justify the infrastructure complexity
- You want to embed workflows in an existing Python app
- Your team is small and ops overhead matters

**Choose Temporal over Stent when:**
- You need enterprise features (versioning, visibility, multi-language)
- You have dedicated infrastructure/DevOps teams
- You're building mission-critical financial systems

---

### vs Celery / Dramatiq

[Celery](https://docs.celeryq.dev) and [Dramatiq](https://dramatiq.io) are task queues. They distribute and retry individual tasks, but don't provide true workflow durability.

**The key difference:**

```python
# Celery - if worker crashes here, workflow state is lost
@app.task
def workflow():
    result1 = step1.delay().get()  # Completed
    result2 = step2.delay().get()  # Worker crashes here
    result3 = step3.delay().get()  # Never reached, no recovery
    return result3
```

```python
# Stent - workflow resumes from last completed step
@Stent.durable()
async def workflow():
    result1 = await step1()  # Completed and persisted
    result2 = await step2()  # Worker crashes, but on restart...
    result3 = await step3()  # ...continues from here
    return result3
```

**Other differences:**

| Aspect | Celery/Dramatiq | Stent |
|--------|-----------------|---------|
| Message broker | Required (RabbitMQ/Redis) | Not needed |
| Async support | Limited (gevent/eventlet) | Native async/await |
| Workflow state | Lost on crash | Persisted |
| Task chaining | Canvas primitives | Native Python |

**Choose Stent over Celery/Dramatiq when:**
- Workflows must survive crashes mid-execution
- You want native async/await
- You don't want to manage a message broker

**Choose Celery/Dramatiq over Stent when:**
- You only need simple fire-and-forget tasks
- You already have RabbitMQ/Redis infrastructure
- You need Celery's mature ecosystem (beat, flower, etc.)

---

### vs Prefect / Airflow

[Prefect](https://prefect.io) and [Apache Airflow](https://airflow.apache.org) are designed for data pipelines and scheduled batch jobs.

**Key philosophical difference:**

- **Airflow/Prefect**: DAG-based, schedule-driven, batch-oriented
- **Stent**: Code-first, event-driven, application-oriented

```python
# Airflow - DAG defines structure, scheduled execution
with DAG("etl_pipeline", schedule="@daily") as dag:
    extract = PythonOperator(task_id="extract", python_callable=extract_fn)
    transform = PythonOperator(task_id="transform", python_callable=transform_fn)
    load = PythonOperator(task_id="load", python_callable=load_fn)
    extract >> transform >> load
```

```python
# Stent - regular async functions, triggered by events
@Stent.durable()
async def process_order(order: dict) -> Result:
    validated = await validate_order(order)
    charged = await charge_payment(validated)
    shipped = await create_shipment(charged)
    return Result.Ok(shipped)

# Triggered by API call, not schedule
await executor.dispatch(process_order, order_data)
```

**Choose Stent over Prefect/Airflow when:**
- Building application workflows (order processing, user onboarding)
- Workflows are triggered by events, not schedules
- You want minimal infrastructure

**Choose Prefect/Airflow over Stent when:**
- Building data pipelines and ETL jobs
- You need scheduled batch processing
- You want a visual DAG UI

---

### vs Azure Durable Functions

[Azure Durable Functions](https://docs.microsoft.com/azure/azure-functions/durable/) provides similar durability but is Azure-specific and uses generator syntax.

```python
# Azure Durable Functions - generator-based, Azure-only
@main.orchestration_trigger(context_name="context")
def orchestrator(context: df.DurableOrchestrationContext):
    result1 = yield context.call_activity("Step1", input1)
    result2 = yield context.call_activity("Step2", result1)
    return result2
```

```python
# Stent - native async, any infrastructure
@Stent.durable()
async def orchestrator(input1):
    result1 = await step1(input1)
    result2 = await step2(result1)
    return result2
```

**Choose Stent over Azure DF when:**
- You're not on Azure
- You want standard async/await instead of generators
- You want to run on your own infrastructure

**Choose Azure DF over Stent when:**
- You're already on Azure Functions
- You want serverless scaling
- You need Azure's managed infrastructure

---

### vs Restate

[Restate](https://restate.dev) is the closest competitor—lightweight, durable, async-native.

**Key difference:** Restate requires running a separate server binary (written in Rust).

```python
# Restate - requires Restate server
@restate.service
class OrderService:
    @restate.handler
    async def process(self, ctx: Context, order: Order) -> Result:
        # ctx.run() for durable steps
        validated = await ctx.run("validate", lambda: validate(order))
        return await ctx.run("charge", lambda: charge(validated))
```

```python
# Stent - pure Python, no external server
@Stent.durable()
async def process_order(order: Order) -> Result:
    validated = await validate(order)  # Automatically durable
    return await charge(validated)
```

**Choose Stent over Restate when:**
- You want a pure Python solution
- You don't want to run a separate server binary
- You prefer simpler decorator-based API

**Choose Restate over Stent when:**
- You need multi-language support (Restate supports TS, Java, Go)
- You want Restate's virtual objects model
- You prefer Restate's state management approach

---

## Summary: When to Choose Stent

Stent is ideal when you need:

1. **True durability** without Temporal's infrastructure complexity
2. **Native Python async** throughout
3. **Minimal setup** (SQLite for dev, Postgres for prod)
4. **Application workflows** (not data pipelines)
5. **Easy embedding** in existing Python applications

It fills the gap between simple task queues and enterprise workflow platforms.

```
                    Simple                                    Complex
                      │                                          │
    Celery ──── Dramatiq ──── Stent ──── Restate ──── Temporal
                                │
                         You are here
                    (Durability + Simplicity)
```
