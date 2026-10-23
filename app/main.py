"""
App entrypoint: `uvicorn app.main:app`.

Planned contents:
- create_app(): builds the FastAPI instance.
- lifespan:
    1. create the DB tables (Alembic replaces this in Stage 5)
    2. modules.auth.services.seed_client(): stores the hash of FETCHR_SEED_CODE
       for FETCHR_CLIENT_ID if no credential row exists yet (the hardcoded first code)
- app.add_middleware(RollingCodeMiddleware) from app/middlewares/auth.py
- include api.v1.router (prefix /api/v1)
- GET /health -> {"status": "ok"} (exempt from auth)
"""
