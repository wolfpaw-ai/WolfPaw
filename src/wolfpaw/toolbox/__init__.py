"""Toolbox: agent-callable tools.

Import this package to register the v1 starter set with the global
registry. Each `tools/<name>.py` module defines a `Tool` subclass and
registers itself at module load.
"""

from wolfpaw.toolbox import registry  # noqa: F401  re-export

# Trigger tool-module imports for side-effect registration. Order doesn't
# matter — the registry deduplicates by `name`.
from wolfpaw.toolbox.tools import (  # noqa: F401
    calculator,
    create_chart,
    create_pdf,
    create_slides,
    create_spreadsheet,
    create_table,
    http_get,
    install_package,
    read_doc,
    run_python,
    sandbox_read_file,
    sandbox_write_file,
    sql_query,
    web_search,
    write_doc,
)

__all__ = ["registry"]
