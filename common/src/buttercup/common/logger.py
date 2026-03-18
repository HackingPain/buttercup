import os

try:
    from opentelemetry._logs import set_logger_provider

    if os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    else:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter  # type: ignore

    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    _opentelemetry_enabled = True
except ImportError:
    _opentelemetry_enabled = False

import logging
import tempfile
from typing import Any

from buttercup.common.telemetry import crs_instance_id, service_instance_id

_is_initialized = False
PACKAGE_LOGGER_NAME = "buttercup"

# ---------------------------------------------------------------------------
# Common structured logging field name constants
# ---------------------------------------------------------------------------
# Use these as keyword argument names when calling log_event() to ensure
# consistent field naming across components.

TASK_ID = "task_id"
COMPONENT = "component"
OPERATION = "operation"
ERROR = "error"
HARNESS = "harness"
PACKAGE = "package"
BUILD_TYPE = "build_type"
SANITIZER = "sanitizer"
ENGINE = "engine"
TARGET = "target"
FILE_PATH = "file_path"
SUBMISSION_ID = "submission_id"
BUNDLE_ID = "bundle_id"
PATCH_ID = "patch_id"
POV_ID = "pov_id"
STATUS = "status"
DURATION = "duration"


def _format_value(value: Any) -> str:
    """Format a single context value for structured log output.

    Strings containing spaces or pipe characters are quoted so that the
    ``key=value`` pairs remain unambiguous when parsed by log aggregators.
    """
    s = str(value)
    if " " in s or "|" in s or "=" in s:
        return f'"{s}"'
    return s


def log_event(logger: logging.Logger, level: int, message: str, **context: Any) -> None:
    """Emit a structured log line in ``message | key1=value1 key2=value2`` format.

    This helper encourages a consistent structured-logging style across all
    Buttercup components.  The *message* should be a short, static description
    of what happened (no f-string interpolation needed).  All variable data
    belongs in *context* keyword arguments so that log aggregation and search
    tools can parse them reliably.

    Examples::

        log_event(logger, logging.INFO, "Processing task",
                  task_id=task.task_id, component="patcher")

        log_event(logger, logging.ERROR, "Patch submission rejected",
                  task_id=task_id, status=response.status, harness=patch)

    Args:
        logger: The :class:`logging.Logger` instance to use.
        level: Numeric log level (e.g. ``logging.INFO``).
        message: A short, human-readable description of the event.
        **context: Arbitrary key-value pairs appended after the message.
    """
    if context:
        pairs = " ".join(f"{k}={_format_value(v)}" for k, v in context.items())
        logger.log(level, "%s | %s", message, pairs)
    else:
        logger.log(level, "%s", message)


class MaxLengthFormatter(logging.Formatter):
    def __init__(self, max_length: int | None = None):
        super().__init__("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        self.max_length = max_length

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if self.max_length:
            msg = msg[: self.max_length]
        return msg


def setup_package_logger(
    application_name: str,
    logger_name: str,
    log_level: str = "info",
    max_line_length: int | None = None,
) -> logging.Logger:
    global _is_initialized

    if not _is_initialized:
        # Clear any existing handlers to avoid duplicates
        root = logging.getLogger()
        if root.handlers:
            for handler in root.handlers:
                root.removeHandler(handler)

        # Create resource with service and environment information
        otlp_handler = None
        if _opentelemetry_enabled:
            resource = Resource.create(
                attributes={
                    "service.name": application_name,
                    "service.instance.id": service_instance_id,
                    "crs.instance.id": crs_instance_id,
                }
            )

            # Initialize the LoggerProvider with the created resource.
            logger_provider = LoggerProvider(resource=resource)

            # Configure the span exporter and processor based on whether the endpoint is effectively set.
            if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
                set_logger_provider(logger_provider)
                exporter = OTLPLogExporter()

                # add the batch processors to the trace provider
                logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
                otlp_handler = LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider)

        persistent_log_dir = os.getenv("PERSISTENT_LOG_DIR", None)

        handlers: list[logging.Handler] = [
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(tempfile.gettempdir(), f"{logger_name}.log")),
        ]
        if persistent_log_dir:
            if not os.path.exists(persistent_log_dir):
                os.makedirs(persistent_log_dir, exist_ok=True)

            handlers.append(logging.FileHandler(os.path.join(persistent_log_dir, f"{logger_name}.log")))

        if otlp_handler:
            handlers.append(otlp_handler)

        for handler in handlers:
            handler.setFormatter(MaxLengthFormatter(max_length=max_line_length))

        # Configure root logger
        logging.basicConfig(
            handlers=handlers,
        )

        _package_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        _package_logger.setLevel(log_level.upper())

        _is_initialized = True

    return logging.getLogger(PACKAGE_LOGGER_NAME)
