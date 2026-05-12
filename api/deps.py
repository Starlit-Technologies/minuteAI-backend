# Backward-compat shim — import from middleware.auth instead.
from middleware.auth import (  # noqa: F401
    get_db,
    get_current_user,
    get_admin_user,
    get_optional_user,
)
