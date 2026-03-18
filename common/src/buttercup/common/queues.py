from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Generic, Literal, TypeVar, cast, overload

from google.protobuf.message import Message
from redis import Redis, RedisError

from buttercup.common.datastructures.msg_pb2 import (
    BuildOutput,
    BuildRequest,
    ConfirmedVulnerability,
    Crash,
    IndexOutput,
    IndexRequest,
    Patch,
    POVReproduceRequest,
    POVReproduceResponse,
    TaskDelete,
    TaskDownload,
    TaskReady,
    TracedCrash,
)
from buttercup.common.redis_pool import get_redis_client

# ruff: noqa: UP046

TIMES_DELIVERED_FIELD = "times_delivered"

DEFAULT_MAX_RETRIES = 3
DLQ_SUFFIX = "_dlq"

F = TypeVar("F", bound=Callable[..., Any])


class QueueNames(str, Enum):
    BUILD = "fuzzer_build_queue"
    BUILD_OUTPUT = "fuzzer_build_output_queue"
    CRASH = "fuzzer_crash_queue"
    CONFIRMED_VULNERABILITIES = "confirmed_vulnerabilities_queue"
    DOWNLOAD_TASKS = "orchestrator_download_tasks_queue"
    READY_TASKS = "tasks_ready_queue"
    DELETE_TASK = "orchestrator_delete_task_queue"
    PATCHES = "patches_queue"
    INDEX = "index_queue"
    INDEX_OUTPUT = "index_output_queue"
    TRACED_VULNERABILITIES = "traced_vulnerabilities_queue"
    POV_REPRODUCER_REQUESTS = "pov_reproducer_requests_queue"
    POV_REPRODUCER_RESPONSES = "pov_reproducer_responses_queue"


class GroupNames(str, Enum):
    BUILDER_BOT = "build_bot_consumers"
    ORCHESTRATOR = "orchestrator_group"
    PATCHER = "patcher_group"
    INDEX = "index_group"
    TRACER_BOT = "tracer_bot_group"


class HashNames(str, Enum):
    TASKS_REGISTRY = "tasks_registry"


BUILD_TASK_TIMEOUT_MS = int(os.getenv("BUILD_TASK_TIMEOUT_MS", 15 * 60 * 1000))
BUILD_OUTPUT_TASK_TIMEOUT_MS = int(os.getenv("BUILD_OUTPUT_TASK_TIMEOUT_MS", 3 * 60 * 1000))
DOWNLOAD_TASK_TIMEOUT_MS = int(os.getenv("DOWNLOAD_TASK_TIMEOUT_MS", 10 * 60 * 1000))
READY_TASK_TIMEOUT_MS = int(os.getenv("READY_TASK_TIMEOUT_MS", 3 * 60 * 1000))
DELETE_TASK_TIMEOUT_MS = int(os.getenv("DELETE_TASK_TIMEOUT_MS", 5 * 60 * 1000))
# Shorter timeout for crashes we want to retry builds fairly quickly since
# all we do in these tasks is reproduce
CRASH_TASK_TIMEOUT_MS = int(os.getenv("CRASH_TASK_TIMEOUT_MS", 4 * 60 * 1000))
PATCH_TASK_TIMEOUT_MS = int(os.getenv("PATCH_TASK_TIMEOUT_MS", 10 * 60 * 1000))
CONFIRMED_VULNERABILITIES_TASK_TIMEOUT_MS = int(os.getenv("CONFIRMED_VULNERABILITIES_TASK_TIMEOUT_MS", 10 * 60 * 1000))
INDEX_TASK_TIMEOUT_MS = int(os.getenv("INDEX_TASK_TIMEOUT_MS", 30 * 60 * 1000))
INDEX_OUTPUT_TASK_TIMEOUT_MS = int(os.getenv("INDEX_OUTPUT_TASK_TIMEOUT_MS", 3 * 60 * 1000))
TRACED_VULNERABILITIES_TASK_TIMEOUT_MS = int(os.getenv("TRACED_VULNERABILITIES_TASK_TIMEOUT_MS", 10 * 60 * 1000))
POV_REPRODUCER_REQUESTS_TASK_TIMEOUT_MS = int(os.getenv("POV_REPRODUCER_REQUESTS_TASK_TIMEOUT_MS", 10 * 60 * 1000))
POV_REPRODUCER_RESPONSES_TASK_TIMEOUT_MS = int(os.getenv("POV_REPRODUCER_RESPONSES_TASK_TIMEOUT_MS", 10 * 60 * 1000))

logger = logging.getLogger(__name__)


# Type variable for protobuf Message subclasses
# Used for type-hinting of reliable queue items
MsgType = TypeVar("MsgType", bound=Message)


@dataclass
class RQItem(Generic[MsgType]):
    """A single item in a reliable queue."""

    item_id: str
    deserialized: MsgType


@dataclass
class ReliableQueue(Generic[MsgType]):
    """A queue that is reliable and can be used to process tasks in a distributed environment."""

    redis: Redis
    queue_name: str
    msg_builder: type[MsgType]
    group_name: str | None = None
    task_timeout_ms: int = 180000
    reader_name: str | None = None
    last_stream_id: str = ">"
    block_time: int | None = 200
    max_retries: int = DEFAULT_MAX_RETRIES

    INAME = b"item"
    DLQ_ITEM = b"item"
    DLQ_ERROR = b"error"
    DLQ_RETRY_COUNT = b"retry_count"
    DLQ_TIMESTAMP = b"timestamp"
    DLQ_ORIGINAL_QUEUE = b"original_queue"
    DLQ_ORIGINAL_ID = b"original_id"

    def __post_init__(self) -> None:
        if self.reader_name is None:
            self.reader_name = f"rqueue_{uuid.uuid4()!s}"

        if self.group_name is not None:
            # Create consumer group if it doesn't exist
            try:
                self.redis.xgroup_create(self.queue_name, self.group_name, mkstream=True, id="0")
            except RedisError as e:
                # Group may already exist
                if "BUSYGROUP Consumer Group name already exists" in str(e):
                    pass
                else:
                    logger.exception(
                        "Failed to create consumer group %s for queue %s",
                        self.group_name,
                        self.queue_name,
                    )

    def size(self) -> int:
        return self.redis.xlen(self.queue_name)

    def push(self, item: MsgType) -> None:
        bts = item.SerializeToString()
        self.redis.xadd(self.queue_name, {self.INAME: bts})

    @staticmethod
    def _ensure_group_name(func: F) -> F:
        @wraps(func)
        def wrapper(self: ReliableQueue[MsgType], *args: Any, **kwargs: Any) -> Any:
            if self.group_name is None:
                raise ValueError("group_name must be set for this operation")

            return func(self, *args, **kwargs)

        return cast("F", wrapper)

    @_ensure_group_name
    def pop(self) -> RQItem[MsgType] | None:
        assert self.group_name
        assert self.reader_name

        streams_items = self.redis.xreadgroup(
            self.group_name,
            self.reader_name,
            {self.queue_name: self.last_stream_id},
            block=self.block_time,
            count=1,
        )
        # Redis xreadgroup returns a list of [stream_name, [(message_id, {field: value})]]
        if streams_items is None or len(streams_items) == 0:
            # No message found in the pending/regular queue for this reader.
            # Try to autoclaim a message
            res = self.redis.xautoclaim(
                self.queue_name,
                self.group_name,
                self.reader_name,
                min_idle_time=self.task_timeout_ms,
                count=1,
            )
            if res is None or len(res[1]) == 0:
                return None

            stream_item = res[1]
        else:
            stream_item = streams_items[0][1]

        if len(stream_item) == 0 and self.last_stream_id != ">":
            # If the queue was created with a last_stream_id that is not `>`, it
            # means the pending items for this reader were desired. In case
            # that's the case and no items were found in the pending queue, look
            # at new messages
            self.last_stream_id = ">"
            return self.pop()

        # Extract message ID and data
        message_id = stream_item[0][0]
        message_data = stream_item[0][1]

        # Create and parse protobuf message
        msg = self.msg_builder()
        msg.ParseFromString(message_data[self.INAME])

        return RQItem[MsgType](item_id=message_id, deserialized=msg)

    @_ensure_group_name
    def ack_item(self, item_id: str) -> None:
        self.redis.xack(self.queue_name, self.group_name, item_id)

    @_ensure_group_name
    def times_delivered(self, item_id: str) -> int:
        pending = self.redis.xpending_range(self.queue_name, self.group_name, item_id, item_id, count=1)
        if pending is None or len(pending) == 0:
            return 0

        return cast("int", pending[0][TIMES_DELIVERED_FIELD])

    @_ensure_group_name
    def claim_item(self, item_id: str, min_idle_time: int = 0) -> None:
        self.redis.xclaim(self.queue_name, self.group_name, self.reader_name, min_idle_time, [item_id])

    @property
    def dlq_name(self) -> str:
        """Return the dead letter queue name for this queue."""
        return f"{self.queue_name}{DLQ_SUFFIX}"

    def should_move_to_dlq(self, item_id: str) -> bool:
        """Check whether a message has exceeded the maximum retry count.

        This requires a consumer group to be configured (uses ``times_delivered``
        internally).  Returns ``False`` when no group is set so callers can
        safely invoke this without guarding on group membership.
        """
        if self.group_name is None:
            return False
        return self.times_delivered(item_id) > self.max_retries

    @_ensure_group_name
    def move_to_dlq(self, item_id: str, error: str = "") -> None:
        """Move a message to the dead letter queue.

        The original message payload is preserved alongside failure metadata
        (timestamp, error description, retry count, originating queue and
        message id).  The message is acknowledged in the source queue after
        being written to the DLQ so it will no longer be re-delivered.

        Args:
            item_id: The stream message id to move.
            error: An optional human-readable error description.
        """
        assert self.group_name

        # Read the original message data from the stream
        messages = self.redis.xrange(self.queue_name, min=item_id, max=item_id, count=1)
        if not messages:
            logger.warning("Cannot move message %s to DLQ: message not found in %s", item_id, self.queue_name)
            return

        original_data = messages[0][1]
        retry_count = self.times_delivered(item_id)

        dlq_entry: dict[bytes, bytes | str | int | float] = {
            self.DLQ_ITEM: original_data[self.INAME],
            self.DLQ_ERROR: error,
            self.DLQ_RETRY_COUNT: retry_count,
            self.DLQ_TIMESTAMP: time.time(),
            self.DLQ_ORIGINAL_QUEUE: self.queue_name,
            self.DLQ_ORIGINAL_ID: item_id,
        }

        self.redis.xadd(self.dlq_name, dlq_entry)

        # Acknowledge in the source queue so it stops being retried
        self.ack_item(item_id)

        logger.info(
            "Moved message %s from %s to DLQ %s (retries=%d, error=%s)",
            item_id,
            self.queue_name,
            self.dlq_name,
            retry_count,
            error,
        )

    def get_dlq_depth(self) -> int:
        """Return the number of messages currently in the dead letter queue."""
        return self.redis.xlen(self.dlq_name)

    def reprocess_dlq(self, count: int = 0) -> int:
        """Move messages from the DLQ back to the main queue for reprocessing.

        Each DLQ entry's original serialized payload is pushed to the main
        queue as a new message and then deleted from the DLQ stream.

        Args:
            count: Maximum number of messages to reprocess.  ``0`` means all.

        Returns:
            The number of messages moved back to the main queue.
        """
        # Read DLQ entries oldest-first
        entries = self.redis.xrange(self.dlq_name, count=count if count > 0 else None)
        if not entries:
            return 0

        moved = 0
        for entry_id, data in entries:
            payload = data.get(self.DLQ_ITEM)
            if payload is None:
                logger.warning("DLQ entry %s in %s missing payload, skipping", entry_id, self.dlq_name)
                continue

            # Re-enqueue in the main stream
            self.redis.xadd(self.queue_name, {self.INAME: payload})
            # Remove from DLQ
            self.redis.xdel(self.dlq_name, entry_id)
            moved += 1

        logger.info("Reprocessed %d messages from DLQ %s back to %s", moved, self.dlq_name, self.queue_name)
        return moved


@dataclass
class QueueConfig:
    queue_name: QueueNames
    msg_builder: type
    task_timeout_ms: int
    group_names: list[GroupNames] = field(default_factory=list)
    max_retries: int = DEFAULT_MAX_RETRIES


@dataclass
class QueueFactory:
    """Factory for creating common reliable queues"""

    redis: Redis
    _config: dict[QueueNames, QueueConfig] = field(
        default_factory=lambda: {
            QueueNames.BUILD: QueueConfig(
                QueueNames.BUILD,
                BuildRequest,
                BUILD_TASK_TIMEOUT_MS,
                [GroupNames.BUILDER_BOT],
            ),
            QueueNames.BUILD_OUTPUT: QueueConfig(
                QueueNames.BUILD_OUTPUT,
                BuildOutput,
                BUILD_OUTPUT_TASK_TIMEOUT_MS,
                [GroupNames.ORCHESTRATOR],
            ),
            QueueNames.DOWNLOAD_TASKS: QueueConfig(
                QueueNames.DOWNLOAD_TASKS,
                TaskDownload,
                DOWNLOAD_TASK_TIMEOUT_MS,
                [GroupNames.ORCHESTRATOR],
            ),
            QueueNames.READY_TASKS: QueueConfig(
                QueueNames.READY_TASKS,
                TaskReady,
                READY_TASK_TIMEOUT_MS,
                [GroupNames.ORCHESTRATOR],
            ),
            QueueNames.CRASH: QueueConfig(
                QueueNames.CRASH,
                Crash,
                CRASH_TASK_TIMEOUT_MS,
                [GroupNames.TRACER_BOT],
            ),
            QueueNames.TRACED_VULNERABILITIES: QueueConfig(
                QueueNames.TRACED_VULNERABILITIES,
                TracedCrash,
                TRACED_VULNERABILITIES_TASK_TIMEOUT_MS,
                [GroupNames.ORCHESTRATOR],
            ),
            QueueNames.CONFIRMED_VULNERABILITIES: QueueConfig(
                QueueNames.CONFIRMED_VULNERABILITIES,
                ConfirmedVulnerability,
                CONFIRMED_VULNERABILITIES_TASK_TIMEOUT_MS,
                [GroupNames.PATCHER],
            ),
            QueueNames.DELETE_TASK: QueueConfig(
                QueueNames.DELETE_TASK,
                TaskDelete,
                DELETE_TASK_TIMEOUT_MS,
                [GroupNames.ORCHESTRATOR],
            ),
            QueueNames.PATCHES: QueueConfig(
                QueueNames.PATCHES,
                Patch,
                PATCH_TASK_TIMEOUT_MS,
                [GroupNames.ORCHESTRATOR],
            ),
            QueueNames.INDEX: QueueConfig(
                QueueNames.INDEX,
                IndexRequest,
                INDEX_TASK_TIMEOUT_MS,
                [GroupNames.INDEX],
            ),
            QueueNames.INDEX_OUTPUT: QueueConfig(
                QueueNames.INDEX_OUTPUT,
                IndexOutput,
                INDEX_OUTPUT_TASK_TIMEOUT_MS,
                [GroupNames.ORCHESTRATOR],
            ),
            QueueNames.POV_REPRODUCER_REQUESTS: QueueConfig(
                QueueNames.POV_REPRODUCER_REQUESTS,
                POVReproduceRequest,
                POV_REPRODUCER_REQUESTS_TASK_TIMEOUT_MS,
                [GroupNames.ORCHESTRATOR],
            ),
            QueueNames.POV_REPRODUCER_RESPONSES: QueueConfig(
                QueueNames.POV_REPRODUCER_RESPONSES,
                POVReproduceResponse,
                POV_REPRODUCER_RESPONSES_TASK_TIMEOUT_MS,
                [GroupNames.ORCHESTRATOR],
            ),
        },
    )

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.BUILD],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[BuildRequest]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.BUILD_OUTPUT],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[BuildOutput]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.DOWNLOAD_TASKS],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[TaskDownload]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.READY_TASKS],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[TaskReady]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.CRASH],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[Crash]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.TRACED_VULNERABILITIES],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[TracedCrash]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.CONFIRMED_VULNERABILITIES],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[ConfirmedVulnerability]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.DELETE_TASK],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[TaskDelete]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.PATCHES],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[Patch]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.INDEX],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[IndexRequest]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.INDEX_OUTPUT],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[IndexOutput]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.POV_REPRODUCER_REQUESTS],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[POVReproduceRequest]: ...

    @overload
    def create(
        self,
        queue_name: Literal[QueueNames.POV_REPRODUCER_RESPONSES],
        group_name: GroupNames,
        **kwargs: Any,
    ) -> ReliableQueue[POVReproduceResponse]: ...

    @overload
    def create(
        self,
        queue_name: QueueNames,
        group_name: GroupNames | None = None,
        **kwargs: Any,
    ) -> ReliableQueue[MsgType]: ...

    def create(
        self,
        queue_name: QueueNames,
        group_name: GroupNames | None = None,
        **kwargs: Any,
    ) -> ReliableQueue[MsgType]:
        if queue_name not in self._config:
            raise ValueError(f"Invalid queue name: {queue_name}")

        config = self._config[queue_name]
        queue_args = {
            "redis": self.redis,
            "queue_name": config.queue_name,
            "msg_builder": config.msg_builder,
            "task_timeout_ms": config.task_timeout_ms,
            "max_retries": config.max_retries,
        }
        if group_name is not None:
            if group_name not in config.group_names:
                raise ValueError(f"Invalid group name: {group_name}")

            queue_args["group_name"] = group_name

        queue_args.update(kwargs)
        return ReliableQueue(**queue_args)  # type: ignore[arg-type]


def create_queue_factory(**redis_kwargs: Any) -> QueueFactory:
    """Create a :class:`QueueFactory` backed by the shared Redis connection pool.

    This is the recommended way to obtain a ``QueueFactory`` instance.  It uses
    :func:`buttercup.common.redis_pool.get_redis_client` so that all queues
    within the process share connections.

    Args:
        **redis_kwargs: Optional overrides forwarded to
            :func:`~buttercup.common.redis_pool.get_redis_client` (e.g.
            ``host``, ``port``, ``db``).

    Returns:
        A :class:`QueueFactory` using the shared pool.
    """
    client = get_redis_client(**redis_kwargs)
    return QueueFactory(redis=client)
