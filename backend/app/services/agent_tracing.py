import logging
import threading
import time
import uuid

from contextlib import contextmanager
from unittest.mock import patch

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.graph.state import CompiledStateGraph

from opentelemetry import trace
from opentelemetry.trace import (
    Status,
    StatusCode,
)

from app.services.observability import (
    build_tracing,
    observed_step,
    persist_trace_event,
    logger,
)


AGENT_NAMES = (
    "financial",
    "news",
    "strategic",
)


class AgentTracingError(RuntimeError):
    pass


def identify_agent(name, tags):
    """
    Identify a real LangGraph agent callback.

    Node names and tags originate from LangGraph,
    not generated business content.
    """
    parts = [str(name or "")]

    if tags:
        parts.extend(
            str(tag)
            for tag in tags
        )

    combined = " ".join(parts).lower()

    for agent in AGENT_NAMES:
        if agent in combined:
            return agent

    return None


class AgentTraceHandler(BaseCallbackHandler):
    """
    LangGraph callback recorder.

    Timings are measured from actual chain-start
    and chain-end callbacks.

    No prompts, article text, model responses,
    or exception messages are persisted.
    """

    def __init__(
        self,
        research_run_id,
        tracer,
        root_span,
    ):
        super().__init__()

        self.research_run_id = research_run_id
        self.tracer = tracer
        self.root_span = root_span

        self.lock = threading.RLock()

        self.active = {}
        self.completed = []
        self.observed_names = set()

    def on_chain_start(
        self,
        serialized,
        inputs,
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        metadata=None,
        **kwargs,
    ):
        name = kwargs.get("name")

        if not name and isinstance(serialized, dict):
            name = serialized.get("name")

            if not name:
                identifier = serialized.get("id", [])

                if isinstance(identifier, list) and identifier:
                    name = identifier[-1]

        agent = identify_agent(name, tags)

        with self.lock:
            if name:
                self.observed_names.add(str(name)[:100])

            if not agent:
                return

            key = str(run_id)

            if key in self.active:
                return

            parent_context = trace.set_span_in_context(
                self.root_span
            )

            span = self.tracer.start_span(
                agent + ".langgraph_execution",
                context=parent_context,
            )

            span.set_attribute(
                "insightiq.run_id",
                self.research_run_id,
            )

            span.set_attribute(
                "insightiq.component",
                agent,
            )

            self.active[key] = {
                "agent": agent,
                "span": span,
                "started": time.perf_counter(),
            }

    def finish(self, run_id, failed=False):
        with self.lock:
            item = self.active.pop(
                str(run_id),
                None,
            )

        if item is None:
            return

        span = item["span"]

        elapsed_ms = (
            time.perf_counter()
            - item["started"]
        ) * 1000.0

        if failed:
            span.set_status(
                Status(StatusCode.ERROR)
            )

        context = span.get_span_context()

        root_context = (
            self.root_span.get_span_context()
        )

        event = persist_trace_event(
            run_id=self.research_run_id,
            trace_id=format(
                context.trace_id,
                "032x",
            ),
            span_id=format(
                context.span_id,
                "016x",
            ),
            parent_span_id=format(
                root_context.span_id,
                "016x",
            ),
            component=item["agent"],
            step_name="langgraph_execution",
            event_type=(
                "step_failed"
                if failed
                else "step_completed"
            ),
            duration_ms=elapsed_ms,
            metadata=None,
        )

        span.end()

        with self.lock:
            self.completed.append(event)

        logger.info({
            "message": "research_trace_event",
            "run_id": self.research_run_id,
            "trace_id": event["trace_id"],
            "span_id": event["span_id"],
            "component": item["agent"],
            "step_name": "langgraph_execution",
            "event_type": event["event_type"],
            "duration_ms": event["duration_ms"],
        })

    def on_chain_end(
        self,
        outputs,
        *,
        run_id,
        **kwargs,
    ):
        self.finish(
            run_id,
            failed=False,
        )

    def on_chain_error(
        self,
        error,
        *,
        run_id,
        **kwargs,
    ):
        # The exception object is never serialized.
        self.finish(
            run_id,
            failed=True,
        )

    def close_open_spans(self):
        """
        Close any callbacks left unfinished after
        an interrupted graph invocation.
        """
        with self.lock:
            outstanding = list(
                self.active.keys()
            )

        for run_id in outstanding:
            self.finish(
                run_id,
                failed=True,
            )


@contextmanager
def traced_graph_invocation(handler):
    """
    Inject tracing callbacks into graph.invoke
    during one controlled workflow execution.

    This scoped patch is intended only for the
    existing single-worker server configuration.
    """
    original_invoke = (
        CompiledStateGraph.invoke
    )

    def instrumented_invoke(
        graph,
        input,
        config=None,
        **kwargs,
    ):
        configuration = dict(
            config or {}
        )

        callbacks = configuration.get(
            "callbacks"
        )

        if callbacks is None:
            configuration["callbacks"] = [
                handler
            ]

        elif isinstance(callbacks, (list, tuple)):
            configuration["callbacks"] = (
                list(callbacks)
                + [handler]
            )

        else:
            # Preserve a callback manager when
            # one is already attached.
            if hasattr(
                callbacks,
                "copy"
            ) and hasattr(
                callbacks,
                "add_handler"
            ):
                copied = callbacks.copy()

                copied.add_handler(
                    handler,
                    inherit=True,
                )

                configuration["callbacks"] = (
                    copied
                )

            else:
                raise AgentTracingError(
                    "Unsupported graph callback configuration."
                )

        return original_invoke(
            graph,
            input,
            config=configuration,
            **kwargs,
        )

    with patch.object(
        CompiledStateGraph,
        "invoke",
        instrumented_invoke,
    ):
        yield


def traced_research_runner(
    graph_runner,
    ticker,
    run_id,
):
    """
    Execute the existing real research workflow,
    preserving its return value.

    Trace data is independently persisted to SQLite.
    """
    tracer, exporter, provider = (
        build_tracing()
    )

    handler = None

    try:
        with observed_step(
            tracer,
            run_id,
            "worker",
            "research_workflow",
        ) as root_span:

            handler = AgentTraceHandler(
                run_id,
                tracer,
                root_span,
            )

            try:
                with traced_graph_invocation(
                    handler
                ):
                    result = graph_runner(
                        ticker,
                        run_id=run_id,
                    )

            finally:
                handler.close_open_spans()

        return result

    finally:
        provider.shutdown()
