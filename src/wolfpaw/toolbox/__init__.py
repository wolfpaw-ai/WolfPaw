"""Toolbox: agent-callable tools.

Import this package to register the v1 starter set with the global
registry. Each `tools/<name>.py` module defines a `Tool` subclass and
registers itself at module load.
"""

from wolfpaw.toolbox import registry  # noqa: F401  re-export

# Trigger tool-module imports for side-effect registration. Order doesn't
# matter — the registry deduplicates by `name`.
from wolfpaw.toolbox.tools import (  # noqa: F401
    ask_user,
    calculator,
    cancel_schedule,
    create_chart,
    create_pdf,
    create_slides,
    create_spreadsheet,
    create_table,
    describe_table,
    http_get,
    install_package,
    list_docs,
    list_schedules,
    list_tables,
    read_doc,
    recall_memory,
    run_python,
    sandbox_read_file,
    sandbox_write_file,
    schedule_task,
    search_docs,
    send_telegram_message,
    sql_delete,
    sql_insert,
    sql_query,
    sql_update,
    update_schedule_state,
    web_search,
    write_doc,
)

__all__ = ["registry"]
