"""Microsoft Calendar integration (v2 step 32).

Microsoft Graph OAuth with Calendars.ReadWrite + offline_access
scopes. Two tools registered on package import:
  * ``outlook_calendar_list_events`` — events in a date range
  * ``outlook_calendar_create_event`` — schedule a new event
"""

from wolfpaw.integrations.microsoft import tools  # noqa: F401
