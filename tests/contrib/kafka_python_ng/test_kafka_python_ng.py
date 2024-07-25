import kafka
from kafka.structs import OffsetAndMetadata
import pytest

from ddtrace import Pin
from ddtrace.contrib.kafka_python_ng.patch import patch
from ddtrace.contrib.kafka_python_ng.patch import unpatch
from ddtrace.filters import TraceFilter
import ddtrace.internal.datastreams  # noqa: F401 - used as part of mock patching
from tests.contrib.config import KAFKA_CONFIG
from tests.utils import DummyTracer
from tests.utils import override_config


GROUP_ID = "test_group"
BOOTSTRAP_SERVERS = "127.0.0.1:{}".format(KAFKA_CONFIG["port"])
KEY = bytes("test_key", encoding="utf-8")
PAYLOAD = bytes("hueh hueh hueh", encoding="utf-8")
DSM_TEST_PATH_HEADER_SIZE = 28


class KafkaConsumerPollFilter(TraceFilter):
    def process_trace(self, trace):
        # Filter out all poll spans that have no received message
        if trace[0].name == "kafka.consume" and trace[0].get_tag("kafka.received_message") == "False":
            return None

        return trace


@pytest.fixture()
def kafka_topic(request):
    topic_name = request.node.name.replace("[", "_").replace("]", "")

    client = kafka.KafkaAdminClient(bootstrap_servers=[BOOTSTRAP_SERVERS])
    try:
        client.create_topics([kafka.admin.NewTopic(topic_name, 1, 1)])
    except kafka.errors.TopicAlreadyExistsError:
        pass
    return topic_name


@pytest.fixture
def producer(tracer):
    _producer = kafka.KafkaProducer(bootstrap_servers=[BOOTSTRAP_SERVERS])
    Pin.override(_producer, tracer=tracer)
    return _producer


@pytest.fixture
def consumer(tracer, kafka_topic):
    _consumer = kafka.KafkaConsumer(
        bootstrap_servers=[BOOTSTRAP_SERVERS], auto_offset_reset="earliest", group_id=GROUP_ID, enable_auto_commit=False
    )
    _consumer.subscribe(topics=[kafka_topic])
    Pin.override(_consumer, tracer=tracer)
    return _consumer


@pytest.fixture
def should_filter_empty_polls():
    yield True


@pytest.fixture
def tracer():
    patch()
    t = DummyTracer()
    # disable backoff because it makes these tests less reliable
    t._writer._send_payload_with_backoff = t._writer._send_payload
    if should_filter_empty_polls:
        t.configure(settings={"FILTERS": [KafkaConsumerPollFilter()]})
    try:
        yield t
    finally:
        t.flush()
        t.shutdown()
        unpatch()


@pytest.fixture
def consumer_without_topic(tracer):
    _consumer = kafka.KafkaConsumer(
        bootstrap_servers=[BOOTSTRAP_SERVERS], auto_offset_reset="earliest", group_id=GROUP_ID, enable_auto_commit=False
    )
    Pin.override(_consumer, tracer=tracer)
    return _consumer


def test_send_single_server(tracer, producer, kafka_topic):
    producer.send(kafka_topic, value=PAYLOAD, key=KEY)
    producer.flush()

    traces = tracer.pop_traces()
    assert 1 == len(traces)
    produce_span = traces[0][0]
    assert produce_span.get_tag("messaging.kafka.bootstrap.servers") == BOOTSTRAP_SERVERS


def test_send_multiple_servers(tracer, kafka_topic):
    producer = kafka.KafkaProducer(bootstrap_servers=[BOOTSTRAP_SERVERS] * 3)
    Pin.override(producer, tracer=tracer)
    producer.send(kafka_topic, value=PAYLOAD, key=KEY)
    producer.flush()

    traces = tracer.pop_traces()
    assert 1 == len(traces)
    produce_span = traces[0][0]
    assert produce_span.get_tag("messaging.kafka.bootstrap.servers") == ",".join([BOOTSTRAP_SERVERS] * 3)
    Pin.override(producer, tracer=None)


def test_send_none_key(tracer, producer, kafka_topic):
    producer.send(kafka_topic, value=PAYLOAD, key=None)
    producer.flush()

    traces = tracer.pop_traces()
    assert 1 == len(traces), "key=None does not cause send() call to raise an exception"
    Pin.override(producer, tracer=None)


@pytest.mark.parametrize("tombstone", [False, True])
@pytest.mark.snapshot(ignores=["metrics.kafka.message_offset"])
def test_message(producer, consumer, tombstone, kafka_topic):
    with override_config("kafka", dict(trace_empty_poll_enabled=False)):
        if tombstone:
            producer.send(kafka_topic, key=KEY)
        else:
            producer.send(kafka_topic, value=PAYLOAD, key=KEY)
        producer.flush()


@pytest.mark.snapshot(ignores=["metrics.kafka.message_offset"])
def test_commit_with_poll(producer, consumer, kafka_topic):
    with override_config("kafka", dict(trace_empty_poll_enabled=False)):
        producer.send(kafka_topic, value=PAYLOAD, key=KEY)
        producer.flush()
        result = consumer.poll(100)
        for topic_partition in result:
            for record in result[topic_partition]:
                consumer.commit({topic_partition: OffsetAndMetadata(record.offset, "")})


@pytest.mark.snapshot(ignores=["metrics.kafka.message_offset"])
def test_commit_with_poll_single_message(tracer, producer, consumer, kafka_topic):
    with override_config("kafka", dict(trace_empty_poll_enabled=False)):
        producer.send(kafka_topic, value=PAYLOAD, key=KEY)
        producer.flush()
        # One message is consumed and one span is generated.
        result = consumer.poll(timeout_ms=100, max_records=1)
        assert len(result) == 1
        topic_partition = list(result.keys())[0]
        assert len(result[topic_partition]) == 1
        consumer.commit({topic_partition: OffsetAndMetadata(result[topic_partition][0].offset, "")})

    traces = tracer.pop_traces()
    assert len(traces) == 2

    span = traces[1][0]
    assert span.name == "kafka.consume"
    assert span.get_tag("kafka.received_message") == "True"


@pytest.mark.snapshot(ignores=["metrics.kafka.message_offset"])
def test_commit_with_poll_with_multiple_messages(tracer, producer, consumer, kafka_topic):
    with override_config("kafka", dict(trace_empty_poll_enabled=False)):
        producer.send(kafka_topic, value=PAYLOAD, key=KEY)
        producer.send(kafka_topic, value=PAYLOAD, key=KEY)
        producer.flush()
        # Two messages are consumed but only ONE span is generated
        result = consumer.poll(timeout_ms=100, max_records=2)
        assert len(result) == 1
        topic_partition = list(result.keys())[0]
        assert len(result[topic_partition]) == 2
        consumer.commit({topic_partition: OffsetAndMetadata(result[topic_partition][1].offset, "")})

    traces = tracer.pop_traces()
    assert len(traces) == 3

    span = traces[2][0]
    assert span.name == "kafka.consume"
    assert span.get_tag("kafka.received_message") == "True"


@pytest.mark.snapshot(ignores=["metrics.kafka.message_offset"])
def test_async_commit(producer, consumer, kafka_topic):
    with override_config("kafka", dict(trace_empty_poll_enabled=False)):
        producer.send(kafka_topic, value=PAYLOAD, key=KEY)
        producer.flush()
        result = consumer.poll(100)
        topic_partition = list(result.keys())[0]
        consumer.commit_async({topic_partition: OffsetAndMetadata(result[topic_partition][1].offset, "")})


# Empty poll should be traced by default
def test_traces_empty_poll_by_default(tracer, consumer, kafka_topic):
    consumer.poll(10.0)

    traces = tracer.pop_traces()

    empty_poll_span_created = False

    for trace in traces:
        for span in trace:
            try:
                assert span.name == "kafka.consume"
                assert span.get_tag("kafka.received_message") == "False"
                empty_poll_span_created = True
            except AssertionError:
                pass

    assert empty_poll_span_created is True


# Empty poll should not be traced when disabled
def test_does_not_trace_empty_poll_when_disabled(tracer, consumer, producer, kafka_topic):
    with override_config("kafka", dict(trace_empty_poll_enabled=False)):
        # Test for empty poll
        consumer.poll(10.0)

        traces = tracer.pop_traces()
        assert 0 == len(traces)

        # Test for non-empty poll right after
        producer.send(kafka_topic, value=PAYLOAD, key=KEY)
        producer.flush()

        result = None
        while result is None:
            result = consumer.poll(10.0)

        traces = tracer.pop_traces()
        non_empty_poll_span_created = False
        for trace in traces:
            for span in trace:
                try:
                    assert span.name == "kafka.consume"
                    assert span.get_tag("kafka.received_message") == "True"
                    non_empty_poll_span_created = True
                except AssertionError:
                    pass

        assert non_empty_poll_span_created is True
