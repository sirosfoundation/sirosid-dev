"""HTTP routes for the assistant. Cookie-authenticated like the rest of the console API, so the
Origin allow-list and the rate limits of Api apply; the turn itself streams server-sent events.

A turn runs in a worker thread (the model call is blocking) and pushes events on a queue; this
module only forwards them. Events: status, tool, tool_result, confirm, message, error, done."""
import json
import queue
import threading
from typing import List

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from .api import SECURITY_HEADERS, Result, status_for
from .chat import ChatService

KEEPALIVE_SECONDS = 15.0


class ChatWeb:
    def __init__(self, api, chat: ChatService):
        self.api, self.chat = api, chat

    def _stream(self, work) -> StreamingResponse:
        q: "queue.Queue" = queue.Queue()

        def run():
            try:
                work(q.put)
            except Exception:                                       # noqa: BLE001 - a turn must always end its stream
                import logging
                logging.getLogger("sirosid.chat").exception("assistant turn failed")
                q.put({"type": "error", "message": "something went wrong"})
            finally:
                q.put(None)
        threading.Thread(target=run, name="chat-turn", daemon=True).start()

        async def events():
            while True:
                try:
                    item = await run_in_threadpool(q.get, True, KEEPALIVE_SECONDS)
                except queue.Empty:
                    yield b": keepalive\n\n"
                    continue
                if item is None:
                    return
                yield b"data: " + json.dumps(item, default=str).encode() + b"\n\n"
        headers = {**SECURITY_HEADERS, "Content-Type": "text/event-stream", "X-Accel-Buffering": "no"}
        return StreamingResponse(events(), headers=headers, media_type="text/event-stream")

    def _endpoint(self, path: str, work_for):
        async def endpoint(request: Request):
            try:
                self.api._origin_ok(request)
                self.api.limiter.check(path, self.api._client(request), 30, 60.0)
                data = await self.api._body(request)
                who = self.api._principal(request)
                if not self.chat.enabled:
                    from .chat import ChatError
                    raise ChatError("the assistant is not enabled on this server")
                self.chat._precheck(who)
            except Exception as e:                                  # noqa: BLE001 - mapped to JSON
                code, payload = status_for(e)
                return self.api._finish(Result(payload, code))
            return self._stream(work_for(who, data))
        return Route(path, endpoint, methods=["POST"])

    def routes(self) -> List[Route]:
        c = self.chat

        def turn(who, d):
            return lambda emit: c.turn(who, d.get("conversation_id") or None, str(d.get("message", "")), d.get("model") or None, emit)

        def confirm(who, d):
            return lambda emit: c.confirm(who, str(d.get("conversation_id", "")), str(d.get("call_id", "")), bool(d.get("approve")), emit)
        return [
            self._endpoint("/api/chat", turn),
            self._endpoint("/api/chat/confirm", confirm),
            self.api.route("/api/chat/status", ["GET"], lambda w, d, p, q: c.status(w)),
            self.api.route("/api/chat/{cid}", ["DELETE"], lambda w, d, p, q: c.end(w, p["cid"]) or {"ok": True}),
        ]
