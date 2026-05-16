from fastapi import FastAPI

app = FastAPI(title="Vault8004 Agent API")


@app.get("/decisions")
async def get_decisions() -> list[dict]:
    raise NotImplementedError


@app.get("/vault")
async def get_vault() -> dict:
    raise NotImplementedError


@app.get("/reputation")
async def get_reputation() -> dict:
    raise NotImplementedError
