"""
Settings(BaseSettings): every config value comes from env / .env (see .env.example).

Planned fields:
- database_url, tmp_dir
- client_id, seed_code, code_grace_seconds
- cookies_file, upload_retries
- b2_endpoint_url, b2_key_id, b2_application_key, b2_bucket, b2_presign_expiry

get_settings(): cached (lru_cache) accessor so settings are read once.
"""
