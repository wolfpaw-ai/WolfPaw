"""Job entry points consumed by the worker runtime.

Each module here exposes a top-level coroutine that takes its arguments
as plain Python types (UUIDs / ints / strings) so the same call shape
works under fire-and-forget ``asyncio.create_task`` today and under
arq's dispatcher in step 23.
"""
