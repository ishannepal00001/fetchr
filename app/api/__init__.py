"""
HTTP layer. Each version (v1, v2, ...) has per-feature folders with:
- schemas.py : request/response validation (Pydantic/SQLModel, NOT tables)
- router.py  : endpoints; thin, they call app/modules/*/services.py
"""
