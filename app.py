import os

import uvicorn

from rikka_tv import create_app


app = create_app()


def _reload_enabled() -> bool:
    value = os.environ.get("LEEUPTV_RELOAD", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=os.environ.get("LEEUPTV_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT") or os.environ.get("LEEUPTV_PORT") or "8000"),
        reload=_reload_enabled(),
    )
