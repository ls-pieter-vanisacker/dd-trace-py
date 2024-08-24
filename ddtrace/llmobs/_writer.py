import atexit
import json
from typing import Any
from typing import Dict
from typing import List
from typing import Union


# TypedDict was added to typing in python 3.8
try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict
from ddtrace import config
from ddtrace.internal import agent
from ddtrace.internal import forksafe
from ddtrace.internal import service
from ddtrace.internal._encoding import BufferedEncoder
from ddtrace.internal.compat import get_connection_response
from ddtrace.internal.compat import httplib
from ddtrace.internal.logger import get_logger
from ddtrace.internal.periodic import PeriodicService
from ddtrace.internal.writer import HTTPWriter
from ddtrace.internal.writer import WriterClientBase
from ddtrace.llmobs._constants import AGENTLESS_BASE_URL
from ddtrace.llmobs._constants import AGENTLESS_ENDPOINT
from ddtrace.llmobs._constants import DROPPED_IO_COLLECTION_ERROR
from ddtrace.llmobs._constants import DROPPED_VALUE_TEXT
from ddtrace.llmobs._constants import EVP_EVENT_SIZE_LIMIT
from ddtrace.llmobs._constants import EVP_PAYLOAD_SIZE_LIMIT
from ddtrace.llmobs._constants import EVP_PROXY_AGENT_ENDPOINT
from ddtrace.llmobs._constants import EVP_SUBDOMAIN_HEADER_NAME
from ddtrace.llmobs._constants import EVP_SUBDOMAIN_HEADER_VALUE


logger = get_logger(__name__)


class LLMObsSpanEvent(TypedDict):
    span_id: str
    trace_id: str
    parent_id: str
    session_id: str
    tags: List[str]
    service: str
    name: str
    start_ns: int
    duration: float
    status: str
    status_message: str
    meta: Dict[str, Any]
    metrics: Dict[str, Any]
    collection_errors: List[str]


class LLMObsEvaluationMetricEvent(TypedDict, total=False):
    span_id: str
    trace_id: str
    metric_type: str
    label: str
    categorical_value: str
    numerical_value: float
    score_value: float
    ml_app: str
    timestamp_ms: int
    tags: List[str]


class BaseLLMObsWriter(PeriodicService):
    """Base writer class for submitting data to Datadog LLMObs endpoints."""

    def __init__(self, site: str, api_key: str, interval: float, timeout: float) -> None:
        super(BaseLLMObsWriter, self).__init__(interval=interval)
        self._lock = forksafe.RLock()
        self._buffer = []  # type: List[Union[LLMObsSpanEvent, LLMObsEvaluationMetricEvent]]
        self._buffer_limit = 1000
        self._timeout = timeout  # type: float
        self._api_key = api_key or ""  # type: str
        self._endpoint = ""  # type: str
        self._site = site  # type: str
        self._intake = ""  # type: str
        self._headers = {"DD-API-KEY": self._api_key, "Content-Type": "application/json"}
        self._event_type = ""  # type: str

    def start(self, *args, **kwargs):
        super(BaseLLMObsWriter, self).start()
        logger.debug("started %r to %r", self.__class__.__name__, self._url)
        atexit.register(self.on_shutdown)

    def on_shutdown(self):
        self.periodic()

    def _enqueue(self, event: Union[LLMObsSpanEvent, LLMObsEvaluationMetricEvent]) -> None:
        with self._lock:
            if len(self._buffer) >= self._buffer_limit:
                logger.warning(
                    "%r event buffer full (limit is %d), dropping event", self.__class__.__name__, self._buffer_limit
                )
                return
            self._buffer.append(event)

    def periodic(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            events = self._buffer
            self._buffer = []

        data = self._data(events)
        try:
            enc_llm_events = json.dumps(data)
        except TypeError:
            logger.error("failed to encode %d LLMObs %s events", len(events), self._event_type, exc_info=True)
            return
        conn = httplib.HTTPSConnection(self._intake, 443, timeout=self._timeout)
        try:
            conn.request("POST", self._endpoint, enc_llm_events, self._headers)
            resp = get_connection_response(conn)
            if resp.status >= 300:
                logger.error(
                    "failed to send %d LLMObs %s events to %s, got response code %d, status: %s",
                    len(events),
                    self._event_type,
                    self._url,
                    resp.status,
                    resp.read(),
                )
            else:
                logger.debug("sent %d LLMObs %s events to %s", len(events), self._event_type, self._url)
        except Exception:
            logger.error(
                "failed to send %d LLMObs %s events to %s", len(events), self._event_type, self._intake, exc_info=True
            )
        finally:
            conn.close()

    @property
    def _url(self) -> str:
        return "https://%s%s" % (self._intake, self._endpoint)

    def _data(self, events: List[Any]) -> Dict[str, Any]:
        raise NotImplementedError


class LLMObsEvalMetricWriter(BaseLLMObsWriter):
    """Writer to the Datadog LLMObs Custom Eval Metrics Endpoint."""

    def __init__(self, site: str, api_key: str, interval: float, timeout: float) -> None:
        super(LLMObsEvalMetricWriter, self).__init__(site, api_key, interval, timeout)
        self._event_type = "evaluation_metric"
        self._buffer = []
        self._endpoint = "/api/intake/llm-obs/v1/eval-metric"
        self._intake = "api.%s" % self._site  # type: str

    def enqueue(self, event: LLMObsEvaluationMetricEvent) -> None:
        self._enqueue(event)

    def _data(self, events: List[LLMObsEvaluationMetricEvent]) -> Dict[str, Any]:
        return {"data": {"type": "evaluation_metric", "attributes": {"metrics": events}}}


class LLMObsSpanEncoder(BufferedEncoder):
    """Encodes LLMObsSpanEvents to JSON in buffer, and is used in LLMObsSpanWriter's LLMObsEventClient"""

    content_type = "application/json"

    def __init__(self, *args):
        super(LLMObsSpanEncoder, self).__init__()
        self._lock = forksafe.RLock()
        self._buffer_limit = 1000
        self._init_buffer()

    def __len__(self):
        with self._lock:
            return len(self._buffer)

    def _init_buffer(self):
        with self._lock:
            self._buffer = []
            self.buffer_size = 0

    def put(self, events: List[LLMObsSpanEvent]):
        # events always has only 1 event - with List type to be compatible with HTTPWriter interfaces
        with self._lock:
            if len(self._buffer) >= self._buffer_limit:
                logger.warning(
                    "%r event buffer full (limit is %d), dropping event", self.__class__.__name__, self._buffer_limit
                )
                return
            self._buffer.extend(events)
            self.buffer_size += len(json.dumps(events))

    def encode(self):
        with self._lock:
            if not self._buffer:
                return
            events = self._buffer
            self._init_buffer()
        data = {"_dd.stage": "raw", "event_type": "span", "spans": events}
        try:
            enc_llm_events = json.dumps(data)
            logger.debug("encode %d LLMObs span events to be sent", len(events))
        except TypeError:
            logger.error("failed to encode %d LLMObs span events", len(events), exc_info=True)
            return
        return enc_llm_events


class LLMObsEventClient(WriterClientBase):
    def __init__(self):
        encoder = LLMObsSpanEncoder(0, 0)
        super(LLMObsEventClient, self).__init__(encoder)


class LLMObsAgentlessEventClient(LLMObsEventClient):
    ENDPOINT = AGENTLESS_ENDPOINT


class LLMObsProxiedEventClient(LLMObsEventClient):
    ENDPOINT = EVP_PROXY_AGENT_ENDPOINT


class LLMObsSpanWriter(HTTPWriter):
    """Writer to the Datadog LLMObs Span Endpoint via Agent EvP Proxy."""

    RETRY_ATTEMPTS = 5
    HTTP_METHOD = "POST"
    STATSD_NAMESPACE = "llmobs.writer"

    def __init__(
        self,
        interval: float,
        timeout: float,
        is_agentless: bool = True,
        dogstatsd=None,
        sync_mode=False,
        reuse_connections=None,
    ):
        headers = {}
        clients = []  # type: List[WriterClientBase]
        if is_agentless:
            clients.append(LLMObsAgentlessEventClient())
            intake_url = "%s.%s" % (AGENTLESS_BASE_URL, config._dd_site)
            headers["DD-API-KEY"] = config._dd_api_key
        else:
            clients.append(LLMObsProxiedEventClient())
            intake_url = agent.get_trace_url()
            headers[EVP_SUBDOMAIN_HEADER_NAME] = EVP_SUBDOMAIN_HEADER_VALUE

        super(LLMObsSpanWriter, self).__init__(
            intake_url=intake_url,
            clients=clients,
            processing_interval=interval,
            timeout=timeout,
            dogstatsd=dogstatsd,
            sync_mode=sync_mode,
            reuse_connections=reuse_connections,
            headers=headers,
        )

    def start(self, *args, **kwargs):
        super(LLMObsSpanWriter, self).start()
        logger.debug("started %r to %r", self.__class__.__name__, self.intake_url)
        atexit.register(self.on_shutdown)

    def stop(self, timeout=None):
        if self.status != service.ServiceStatus.STOPPED:
            super(LLMObsSpanWriter, self).stop(timeout=timeout)

    def enqueue(self, event: LLMObsSpanEvent) -> None:
        event_size = len(json.dumps(event))

        if event_size >= EVP_EVENT_SIZE_LIMIT:
            logger.warning(
                "dropping event input/output because its size (%d) exceeds the event size limit (1MB)",
                event_size,
            )
            event = _truncate_span_event(event)

        for client in self._clients:
            if isinstance(client, LLMObsEventClient) and isinstance(client.encoder, LLMObsSpanEncoder):
                with client.encoder._lock:
                    if (client.encoder.buffer_size + event_size) > EVP_PAYLOAD_SIZE_LIMIT:
                        logger.debug("flushing queue because queuing next event will exceed EVP payload limit")
                        self._flush_queue_with_client(client)
        self.write([event])

    # Noop to make it compatible with HTTPWriter interface
    def _set_keep_rate(self, events: List[LLMObsSpanEvent]):
        return

    def recreate(self):
        # type: () -> HTTPWriter
        return self.__class__(
            interval=self._interval,
            timeout=self._timeout,
            is_agentless=config._llmobs_agentless_enabled,
        )


def _truncate_span_event(event: LLMObsSpanEvent) -> LLMObsSpanEvent:
    event["meta"]["input"] = {"value": DROPPED_VALUE_TEXT}
    event["meta"]["output"] = {"value": DROPPED_VALUE_TEXT}

    event["collection_errors"] = [DROPPED_IO_COLLECTION_ERROR]
    return event
