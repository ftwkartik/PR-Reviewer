from fastapi import FastAPI

from app.api import items, stats

app = FastAPI(title="Demo shop")
app.include_router(items.router)
app.include_router(stats.router)
