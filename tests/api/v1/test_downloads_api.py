"""
/api/v1/downloads endpoint tests (planned):
- POST validates the body (bad url -> 422) and returns 202 with an id + next_code
- GET /{id} for another client's download -> 404
- GET /{id}/file before completed -> 409; after completed -> presigned URL / 307
"""
