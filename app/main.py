"""Application factory: reads the settings, prepares the databases and wires the API.

Run with `.venv\\Scripts\\python -m uvicorn app.main:create_app --factory` (Windows) or
`.venv/bin/python -m uvicorn app.main:create_app --factory` (macOS / Linux); see the README.
Add `--reload` while editing the code, to restart on every change.
"""

import json
import logging
from contextlib import closing

from fastapi import FastAPI

from app import __version__, storage
from app.api import router
from app.config import Settings
from app.llm import build_llm_client
from app.reference import load_reference_data

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Uvicorn calls it without arguments; tests pass their own settings."""
    if settings is None:
        settings = Settings()
    configure_logging(settings.log_level)

    llm = build_llm_client(settings)  # fails fast on a missing provider or API key
    reference = load_reference_data(settings.data_dir / "reference_data.json")
    history_path = settings.data_dir / "historical_resolutions.json"
    history_rows = json.loads(history_path.read_text(encoding="utf-8"))
    with closing(storage.connect(settings.operational_db_path)) as conn:
        storage.init_operational_db(conn)
        new_history_rows = storage.seed_history(conn, history_rows)
    with closing(storage.connect(settings.analytics_db_path)) as conn:
        storage.init_analytics_db(conn)

    app = FastAPI(
        title="LedgerRoute",
        summary="Auditable invoice classification and supplier resolution.",
        description=(
            "Normalize post-OCR invoice data, resolve suppliers and spend classifications "
            "with reference and historical evidence, enforce review policies, and export "
            "versioned results for analytics."
        ),
        version=__version__,
    )
    app.state.settings = settings
    app.state.reference = reference
    app.state.llm = llm
    app.include_router(router)
    logger.info("LLM client: provider=%s model=%s", llm.provider, llm.model)
    logger.info(
        "Reference data: %d suppliers, %d categories, %d business units | history: %d rows, %d new",
        len(reference.canonical_suppliers),
        len(reference.categories),
        len(reference.business_units),
        len(history_rows),
        new_history_rows,
    )
    logger.info(
        "LedgerRoute ready | operational db: %s | analytics db: %s",
        settings.operational_db_path,
        settings.analytics_db_path,
    )
    return app


def configure_logging(level: str) -> None:
    """One readable log format for the whole service.

    `basicConfig` does nothing if the root logger already has handlers (for example under
    pytest), so test log capture keeps working.
    """
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    # httpx2 logs every HTTP request at INFO; app.llm already logs one line per model call.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
