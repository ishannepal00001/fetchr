"""
modules/auth/services tests (planned):
- seed_client is idempotent
- verify_and_rotate returns a new code and invalidates the old one
- grace window: the previous code is accepted once within the window, rejected after it
- two concurrent verify_and_rotate calls with the same code -> exactly one succeeds
- reset_code replaces the code
"""
