"""Unit tests for dead letter queue (DLQ) functionality in ReliableQueue.

All tests use mock Redis to avoid requiring a running Redis instance.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest
from google.protobuf.struct_pb2 import Struct

from buttercup.common.queues import (
    DEFAULT_MAX_RETRIES,
    DLQ_SUFFIX,
    GroupNames,
    QueueConfig,
    QueueFactory,
    QueueNames,
    ReliableQueue,
)

GROUP_NAME = "test_group"
QUEUE_NAME = "test_queue"


@pytest.fixture
def mock_redis():
    """Return a MagicMock that behaves enough like a Redis client."""
    r = MagicMock()
    # xgroup_create should not raise by default
    r.xgroup_create.return_value = True
    return r


@pytest.fixture
def queue(mock_redis):
    """A ReliableQueue with a consumer group and max_retries=3."""
    return ReliableQueue[Struct](
        redis=mock_redis,
        queue_name=QUEUE_NAME,
        msg_builder=Struct,
        group_name=GROUP_NAME,
        task_timeout_ms=1000,
        reader_name="test_reader",
        max_retries=3,
    )


# ---------------------------------------------------------------------------
# dlq_name property
# ---------------------------------------------------------------------------


def test_dlq_name(queue):
    assert queue.dlq_name == f"{QUEUE_NAME}{DLQ_SUFFIX}"


def test_dlq_name_uses_suffix_constant():
    """Verify the DLQ name is derived from the queue name + DLQ_SUFFIX."""
    q = ReliableQueue[Struct](
        redis=MagicMock(),
        queue_name="my_custom_queue",
        msg_builder=Struct,
    )
    assert q.dlq_name == f"my_custom_queue{DLQ_SUFFIX}"


# ---------------------------------------------------------------------------
# should_move_to_dlq
# ---------------------------------------------------------------------------


def test_should_move_to_dlq_false_when_no_group():
    """Without a consumer group, should_move_to_dlq always returns False."""
    q = ReliableQueue[Struct](
        redis=MagicMock(),
        queue_name=QUEUE_NAME,
        msg_builder=Struct,
        max_retries=3,
    )
    assert q.should_move_to_dlq("some-id") is False


def test_should_move_to_dlq_below_threshold(queue, mock_redis):
    """Returns False when times_delivered <= max_retries."""
    mock_redis.xpending_range.return_value = [{"times_delivered": 2}]
    assert queue.should_move_to_dlq("msg-1") is False


def test_should_move_to_dlq_at_threshold(queue, mock_redis):
    """Returns False when times_delivered == max_retries (not exceeded yet)."""
    mock_redis.xpending_range.return_value = [{"times_delivered": 3}]
    assert queue.should_move_to_dlq("msg-1") is False


def test_should_move_to_dlq_above_threshold(queue, mock_redis):
    """Returns True when times_delivered > max_retries."""
    mock_redis.xpending_range.return_value = [{"times_delivered": 4}]
    assert queue.should_move_to_dlq("msg-1") is True


# ---------------------------------------------------------------------------
# move_to_dlq
# ---------------------------------------------------------------------------


def test_move_to_dlq_writes_and_acks(queue, mock_redis):
    """move_to_dlq should xadd to the DLQ stream and xack the original."""
    original_payload = b"serialized-proto"
    mock_redis.xrange.return_value = [
        (b"msg-1", {ReliableQueue.INAME: original_payload}),
    ]
    mock_redis.xpending_range.return_value = [{"times_delivered": 4}]

    queue.move_to_dlq("msg-1", error="Something broke")

    # Verify DLQ entry was written
    dlq_call = mock_redis.xadd.call_args
    assert dlq_call is not None
    dlq_stream = dlq_call[0][0]
    dlq_data = dlq_call[0][1]

    assert dlq_stream == queue.dlq_name
    assert dlq_data[ReliableQueue.DLQ_ITEM] == original_payload
    assert dlq_data[ReliableQueue.DLQ_ERROR] == "Something broke"
    assert dlq_data[ReliableQueue.DLQ_RETRY_COUNT] == 4
    assert dlq_data[ReliableQueue.DLQ_ORIGINAL_QUEUE] == QUEUE_NAME
    assert dlq_data[ReliableQueue.DLQ_ORIGINAL_ID] == "msg-1"
    # timestamp should be a float
    assert isinstance(dlq_data[ReliableQueue.DLQ_TIMESTAMP], float)

    # Verify original was acknowledged
    mock_redis.xack.assert_called_once_with(QUEUE_NAME, GROUP_NAME, "msg-1")


def test_move_to_dlq_missing_message_is_noop(queue, mock_redis):
    """When the message is not found in the stream, nothing happens."""
    mock_redis.xrange.return_value = []

    queue.move_to_dlq("nonexistent-id", error="gone")

    mock_redis.xadd.assert_not_called()
    mock_redis.xack.assert_not_called()


def test_move_to_dlq_requires_group():
    """move_to_dlq raises when no consumer group is configured."""
    q = ReliableQueue[Struct](
        redis=MagicMock(),
        queue_name=QUEUE_NAME,
        msg_builder=Struct,
    )
    with pytest.raises(ValueError, match="group_name must be set"):
        q.move_to_dlq("msg-1")


def test_move_to_dlq_default_error_is_empty(queue, mock_redis):
    """When no error is provided the DLQ entry has an empty error string."""
    mock_redis.xrange.return_value = [
        (b"msg-1", {ReliableQueue.INAME: b"data"}),
    ]
    mock_redis.xpending_range.return_value = [{"times_delivered": 5}]

    queue.move_to_dlq("msg-1")

    dlq_data = mock_redis.xadd.call_args[0][1]
    assert dlq_data[ReliableQueue.DLQ_ERROR] == ""


# ---------------------------------------------------------------------------
# get_dlq_depth
# ---------------------------------------------------------------------------


def test_get_dlq_depth(queue, mock_redis):
    mock_redis.xlen.return_value = 42
    assert queue.get_dlq_depth() == 42
    mock_redis.xlen.assert_called_once_with(queue.dlq_name)


def test_get_dlq_depth_empty(queue, mock_redis):
    mock_redis.xlen.return_value = 0
    assert queue.get_dlq_depth() == 0


# ---------------------------------------------------------------------------
# reprocess_dlq
# ---------------------------------------------------------------------------


def test_reprocess_dlq_moves_messages_back(queue, mock_redis):
    """reprocess_dlq should re-enqueue DLQ items and delete them from the DLQ."""
    mock_redis.xrange.return_value = [
        (b"dlq-1", {ReliableQueue.DLQ_ITEM: b"payload-a"}),
        (b"dlq-2", {ReliableQueue.DLQ_ITEM: b"payload-b"}),
    ]

    moved = queue.reprocess_dlq()

    assert moved == 2

    # Should have added both back to the main queue
    add_calls = mock_redis.xadd.call_args_list
    assert len(add_calls) == 2
    assert add_calls[0] == call(QUEUE_NAME, {ReliableQueue.INAME: b"payload-a"})
    assert add_calls[1] == call(QUEUE_NAME, {ReliableQueue.INAME: b"payload-b"})

    # Should have deleted both from DLQ
    del_calls = mock_redis.xdel.call_args_list
    assert len(del_calls) == 2
    assert del_calls[0] == call(queue.dlq_name, b"dlq-1")
    assert del_calls[1] == call(queue.dlq_name, b"dlq-2")


def test_reprocess_dlq_empty(queue, mock_redis):
    mock_redis.xrange.return_value = []
    assert queue.reprocess_dlq() == 0
    mock_redis.xadd.assert_not_called()


def test_reprocess_dlq_with_count(queue, mock_redis):
    """When count > 0, only that many messages should be read."""
    mock_redis.xrange.return_value = [
        (b"dlq-1", {ReliableQueue.DLQ_ITEM: b"payload-a"}),
    ]

    moved = queue.reprocess_dlq(count=1)

    assert moved == 1
    mock_redis.xrange.assert_called_once_with(queue.dlq_name, count=1)


def test_reprocess_dlq_count_zero_reads_all(queue, mock_redis):
    """count=0 (default) reads all entries."""
    mock_redis.xrange.return_value = []

    queue.reprocess_dlq(count=0)

    mock_redis.xrange.assert_called_once_with(queue.dlq_name, count=None)


def test_reprocess_dlq_skips_entries_without_payload(queue, mock_redis):
    """DLQ entries missing the payload field are skipped."""
    mock_redis.xrange.return_value = [
        (b"dlq-1", {b"other_field": b"irrelevant"}),
        (b"dlq-2", {ReliableQueue.DLQ_ITEM: b"good-payload"}),
    ]

    moved = queue.reprocess_dlq()

    assert moved == 1
    mock_redis.xadd.assert_called_once_with(QUEUE_NAME, {ReliableQueue.INAME: b"good-payload"})
    # Only the good entry is deleted from DLQ
    mock_redis.xdel.assert_called_once_with(queue.dlq_name, b"dlq-2")


# ---------------------------------------------------------------------------
# max_retries defaults
# ---------------------------------------------------------------------------


def test_default_max_retries():
    q = ReliableQueue[Struct](
        redis=MagicMock(),
        queue_name=QUEUE_NAME,
        msg_builder=Struct,
    )
    assert q.max_retries == DEFAULT_MAX_RETRIES


def test_custom_max_retries():
    q = ReliableQueue[Struct](
        redis=MagicMock(),
        queue_name=QUEUE_NAME,
        msg_builder=Struct,
        max_retries=10,
    )
    assert q.max_retries == 10


# ---------------------------------------------------------------------------
# QueueConfig max_retries
# ---------------------------------------------------------------------------


def test_queue_config_default_max_retries():
    cfg = QueueConfig(
        queue_name=QueueNames.BUILD,
        msg_builder=Struct,
        task_timeout_ms=1000,
    )
    assert cfg.max_retries == DEFAULT_MAX_RETRIES


def test_queue_config_custom_max_retries():
    cfg = QueueConfig(
        queue_name=QueueNames.BUILD,
        msg_builder=Struct,
        task_timeout_ms=1000,
        max_retries=5,
    )
    assert cfg.max_retries == 5


# ---------------------------------------------------------------------------
# QueueFactory passes max_retries through
# ---------------------------------------------------------------------------


def test_factory_passes_max_retries(mock_redis):
    factory = QueueFactory(redis=mock_redis)
    queue = factory.create(QueueNames.BUILD, GroupNames.BUILDER_BOT)
    assert queue.max_retries == DEFAULT_MAX_RETRIES


def test_factory_custom_max_retries(mock_redis):
    """Override max_retries in the config and verify it propagates."""
    factory = QueueFactory(redis=mock_redis)
    # Patch the config for BUILD to have a custom max_retries
    factory._config[QueueNames.BUILD].max_retries = 7
    queue = factory.create(QueueNames.BUILD, GroupNames.BUILDER_BOT)
    assert queue.max_retries == 7
