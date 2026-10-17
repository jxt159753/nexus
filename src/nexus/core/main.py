
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from nexus.core.database import get_connection, init_database


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    yield


app = FastAPI(title="NEXUS Core", lifespan=lifespan)


class TaskRequest(BaseModel):
    task_type: str = Field(min_length=1)
    target: str = Field(min_length=1)


@app.get("/health")
def health():
    return {
        "system": "NEXUS",
        "status": "online",
        "version": "0.1.0",
    }


@app.post("/tasks")
def create_task(task: TaskRequest):
    task_id = str(uuid4())

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO tasks
            (task_id, task_type, target, status)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, task.task_type, task.target, "received"),
        )

    return {
        "task_id": task_id,
        "task_type": task.task_type,
        "target": task.target,
        "status": "received",
    }


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return dict(row)
