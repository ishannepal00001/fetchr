"""
Shared FastAPI dependencies.

Planned contents:
- get_session: re-exported from app/core/database.py
- current_client_id(request) -> str: returns request.state.client_id, which
  RollingCodeMiddleware set after verifying the code
- next_code(request) -> str: returns request.state.next_code, used to fill CodeEnvelope.next_code
"""
