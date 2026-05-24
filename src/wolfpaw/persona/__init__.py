"""Persona subsystem — the Soul file (Wolfpaw's persona, global) + per-user
User File (one row per user). Every agent prompt is conditioned on both.

Step 17 ships the loaders, the system-prompt builder, REST endpoints
for viewing/editing the user's profile, and version-stamping of threads
on creation. The web UI for editing lands in step 19.

Public surface (use full paths in callers to keep import graph shallow):
    from wolfpaw.persona.soul import get_soul, Soul
    from wolfpaw.persona.user_profile import UserProfile, get, update
    from wolfpaw.persona.builder import build_system_prompt, build_for_agent
"""
