"""The assistant: a scripted fake model drives the real tool table, control plane and HTTP layer."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import HAVE, ORIGIN, Console, build, signed_in  # noqa: E402

if HAVE:
    import httpx
    from sirosid_service.api import ApiConfig, create_app
    from sirosid_service.auth import AuthConfig, AuthService
    from sirosid_service.chat import SYSTEM_PROMPT, ChatConfig, ChatService
    from sirosid_service.llm import LlmError, OpenRouter

NEEDS = unittest.skipUnless(HAVE, "needs starlette, httpx, webauthn, cbor2")


def text(content, tokens=(10, 5)):
    return {"message": {"role": "assistant", "content": content}, "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}}


def call(name, args=None, cid="call_1", raw=None, content=None):
    return {"message": {"role": "assistant", "content": content, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": raw if raw is not None else json.dumps(args or {})}}]},
        "usage": {"prompt_tokens": 20, "completion_tokens": 8}}


class FakeLlm:
    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def complete(self, model, messages, tools, max_tokens=2000):
        self.calls.append({"model": model, "messages": json.loads(json.dumps(messages)), "tools": tools})
        if not self.script:
            raise AssertionError("the model was called more often than the test scripted")
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def sse(resp):
    assert resp.status_code == 200, resp.text
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def make_app(llm, config=None, **invite):
    cp, auth, _, admin, fake, clock = build()
    chat = ChatService(cp, llm, config or ChatConfig(models=("test/model", "test/other")))
    app = create_app(cp, auth, ApiConfig(origins=(ORIGIN,)), chat=chat)
    return cp, chat, app, admin, clock


@NEEDS
class ChatTests(unittest.TestCase):
    def start(self, *script, config=None, **invite):
        self.llm = FakeLlm(*script)
        self.cp, self.chat, self.app, self.admin, self.clock = make_app(self.llm, config)
        self.c = signed_in(self.cp, self.app, self.admin, **invite)
        return self.c

    def say(self, message, conv=None, c=None, **extra):
        return sse((c or self.c).post("/api/chat", {"message": message, **({"conversation_id": conv} if conv else {}), **extra}))

    def confirm(self, conv, call_id, approve, c=None):
        return sse((c or self.c).post("/api/chat/confirm", {"conversation_id": conv, "call_id": call_id, "approve": approve}))

    # ---- the basics ---------------------------------------------------------------------------------

    def test_a_plain_answer_streams_a_message_then_done_and_is_charged(self):
        self.start(text("Hello there"))
        ev = self.say("hi")
        self.assertEqual([e["type"] for e in ev], ["message", "done"])
        self.assertEqual(ev[0]["text"], "Hello there")
        self.assertEqual(ev[1]["usage"], {"used": 15, "limit": 300_000})
        first = self.llm.calls[0]
        self.assertEqual(first["model"], "test/model")
        self.assertEqual(first["messages"][0], {"role": "system", "content": SYSTEM_PROMPT})
        self.assertEqual(first["messages"][1], {"role": "user", "content": "hi"})

    def test_tool_calls_run_as_the_user_through_the_real_tools(self):
        self.start(call("save_config", {"name": "mine", "config": {}}), call("list_configs", cid="call_2"), text("Saved and listed."))
        ev = self.say("save an empty config called mine, then list")
        self.assertEqual([e["type"] for e in ev], ["tool", "tool_result", "tool", "tool_result", "message", "done"])
        self.assertTrue(all(e["ok"] for e in ev if e["type"] == "tool_result"))
        self.assertEqual([c["name"] for c in self.c.get("/api/configs").json()["configs"]], ["mine"], "the console sees what the assistant did")
        tool_msgs = [m for m in self.llm.calls[-1]["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        self.assertIn("mine", tool_msgs[1]["content"])

    def test_the_conversation_continues_with_its_history(self):
        self.start(text("one"), text("two"))
        conv = self.say("first")[-1]["conversation_id"]
        self.say("second", conv)
        roles = [m["role"] for m in self.llm.calls[1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])

    def test_quotas_and_policy_still_bind_the_assistant(self):
        self.start(call("save_config", {"name": "bad", "config": {"mystery": 1}}), text("That config is invalid."))
        ev = self.say("save a bad config")
        self.assertFalse([e for e in ev if e["type"] == "tool_result"][0]["ok"])
        self.assertIn("mystery", self.llm.calls[1]["messages"][-1]["content"])
        self.assertEqual(self.c.get("/api/configs").json()["configs"], [])

    # ---- what the model is and is not given ------------------------------------------------------------

    def test_credentials_and_admin_actions_are_not_offered_and_not_runnable(self):
        self.start(call("get_instance_credentials", {"id": "x"}), call("create_invite", {}, cid="c2"), text("ok"))
        ev = self.say("get me the admin token")
        offered = {t["function"]["name"] for t in self.llm.calls[0]["tools"]}
        self.assertNotIn("get_instance_credentials", offered)
        self.assertFalse([n for n in offered if "invite" in n or "grant" in n or "passkey" in n])
        self.assertIn("create_instance", offered)
        results = [e for e in ev if e["type"] == "tool_result"]
        self.assertTrue(all(not r["ok"] for r in results))
        self.assertIn("no tool named", results[0]["summary"])

    def test_bad_tool_arguments_become_error_results_not_crashes(self):
        self.start(call("get_config", raw="{not json"), call("get_config", {"nope": 1}, cid="c2"), call("get_config", raw="[1]", cid="c3"), text("done"))
        ev = self.say("go")
        results = [e for e in ev if e["type"] == "tool_result"]
        self.assertEqual([r["ok"] for r in results], [False, False, False])
        self.assertEqual(ev[-1]["type"], "done")

    def test_large_tool_output_is_truncated(self):
        self.start(call("get_config_schema"), text("ok"), config=ChatConfig(models=("m",), max_tool_chars=100))
        self.say("schema")
        tool = [m for m in self.llm.calls[1]["messages"] if m["role"] == "tool"][0]["content"]
        self.assertTrue(tool.endswith("[truncated]"))
        self.assertLess(len(tool), 130)

    # ---- destructive actions need the user ------------------------------------------------------------------

    def make_instance(self):
        return self.c.post("/api/instances", {"config": {}}).json()["id"]

    def test_destroy_pauses_for_approval_and_runs_only_after_it(self):
        self.start(text("x"))
        iid = self.make_instance()
        self.llm.script = [call("destroy_instance", {"id": iid}, cid="d1", content="Destroying it."), text("Destroyed.")]
        ev = self.say("destroy it")
        self.assertEqual([e["type"] for e in ev], ["message", "confirm"])
        self.assertEqual((ev[1]["name"], ev[1]["args"]), ("destroy_instance", {"id": iid}))
        self.assertEqual(len(self.c.get("/api/instances").json()["instances"]), 1, "nothing happened yet")
        ev2 = self.confirm(ev[1]["conversation_id"], "d1", True)
        self.assertEqual([e["type"] for e in ev2], ["tool", "tool_result", "message", "done"])
        self.assertEqual(self.c.get("/api/instances").json()["instances"], [])

    def test_denying_runs_nothing_and_tells_the_model(self):
        self.start(text("x"))
        iid = self.make_instance()
        self.llm.script = [call("reset_instance", {"id": iid}, cid="r1"), text("Understood, I left it alone.")]
        ev = self.say("reset it")
        ev2 = self.confirm(ev[-1]["conversation_id"], "r1", False)
        self.assertEqual(ev2[0]["summary"], "declined")
        self.assertEqual(self.c.get(f"/api/instances/{iid}").json()["status"], "running")
        last = self.llm.calls[-1]["messages"][-1]
        self.assertEqual(last["role"], "tool")
        self.assertIn("declined", last["content"])

    def test_every_destructive_tool_needs_approval(self):
        self.start(call("delete_config", {"name": "a"}, cid="a"), call("destroy_instance", {"id": "i"}, cid="b"), call("reset_instance", {"id": "i"}, cid="c"))
        for cid in ("a", "b", "c"):
            self.llm.script = [call({"a": "delete_config", "b": "destroy_instance", "c": "reset_instance"}[cid], {"name": "a", "id": "i"}, cid=cid), text("ok")]
            ev = self.say("do it")
            self.assertEqual(ev[-1]["type"], "confirm", cid)
            self.confirm(ev[-1]["conversation_id"], cid, False)

    def test_approvals_cannot_be_forged_replayed_or_borrowed(self):
        self.start(text("x"))
        iid = self.make_instance()
        self.llm.script = [call("destroy_instance", {"id": iid}, cid="d1"), text("done")]
        ev = self.say("destroy it")
        conv = ev[-1]["conversation_id"]
        wrong = self.confirm(conv, "not-that-call", True)
        self.assertEqual(wrong[0]["type"], "error")
        self.assertEqual(self.say("also this", conv)[0]["type"], "error", "a new message cannot skip a pending approval")
        other = signed_in(self.cp, self.app, self.admin)
        self.assertEqual(self.confirm(conv, "d1", True, c=other)[0]["type"], "error", "another user cannot approve")
        self.assertEqual(len(self.c.get("/api/instances").json()["instances"]), 1)
        self.confirm(conv, "d1", True)
        self.assertEqual(self.confirm(conv, "d1", True)[0]["type"], "error", "an approval is single use")

    def test_text_in_tool_output_cannot_approve_anything(self):
        """An instance label that tells the model to destroy things: the model may obey, the interface still asks."""
        self.start(text("x"))
        iid = self.make_instance()
        self.c.post(f"/api/instances/{iid}/keep", {"keep": False})
        self.llm.script = [call("list_instances"), call("destroy_instance", {"id": iid}, cid="evil"), text("x")]
        ev = self.say("list my instances")
        self.assertEqual(ev[-1]["type"], "confirm")
        self.assertEqual(self.c.get(f"/api/instances/{iid}").json()["status"], "running")

    # ---- limits -----------------------------------------------------------------------------------------------

    def test_user_budget_stops_the_model_being_called(self):
        self.start(text("a", tokens=(60, 50)), config=ChatConfig(models=("m",), user_daily_tokens=100))
        conv = self.say("one")[-1]["conversation_id"]
        calls = len(self.llm.calls)
        ev = self.say("two", conv)
        self.assertEqual(ev[0]["type"], "error")
        self.assertIn("allowance", ev[0]["message"])
        self.assertEqual(len(self.llm.calls), calls)

    def test_global_budget_and_the_daily_reset(self):
        self.start(text("a", tokens=(60, 50)), text("b"), config=ChatConfig(models=("m",), global_daily_tokens=100))
        self.say("one")
        self.assertIn("overall", self.say("two")[0]["message"])
        self.clock.advance(days=1)
        self.c = signed_in(self.cp, self.app, self.admin)          # the 12 h web session lapsed too
        self.assertEqual(self.say("three")[0]["type"], "message")

    def test_the_step_limit_ends_a_runaway_loop(self):
        self.start(*[call("list_configs", cid=f"c{i}") for i in range(10)], config=ChatConfig(models=("m",), max_steps=3))
        ev = self.say("loop forever")
        self.assertEqual(len(self.llm.calls), 3)
        self.assertEqual([e["type"] for e in ev][-2:], ["error", "done"])

    def test_input_limits_and_model_allow_list(self):
        self.start(text("a"), config=ChatConfig(models=("m",), max_message_chars=20))
        self.assertIn("limited", self.say("x" * 21)[0]["message"])
        self.assertEqual(self.say("")[0]["type"], "error")
        self.assertIn("not available", self.say("hi", model="evil/model")[0]["message"])
        self.assertEqual(self.llm.calls, [])

    def test_one_turn_at_a_time_per_user(self):
        self.start(text("a"))
        who = self.cp.principal_for(self.c.get("/api/me").json()["user_id"], "")
        self.chat._running.add(who.user_id)
        self.assertIn("still working", self.say("hi")[0]["message"])
        self.chat._running.clear()

    def test_history_is_bounded_and_never_starts_mid_exchange(self):
        self.start(*[call("list_configs", cid=f"c{i}") if i % 2 == 0 else text("t") for i in range(0)], config=ChatConfig(models=("m",), max_history=4))
        self.llm.script = [call("list_configs", cid="c1"), text("one"), text("two"), text("three")]
        conv = self.say("a")[-1]["conversation_id"]
        self.say("b", conv)
        self.say("c", conv)
        for c in self.llm.calls:
            msgs = c["messages"][1:]
            self.assertEqual(msgs[0]["role"], "user", "a trimmed history must start at a user message")
            ids = {t["id"] for m in msgs for t in m.get("tool_calls", [])}
            self.assertTrue(all(m["tool_call_id"] in ids for m in msgs if m["role"] == "tool"), "no orphaned tool results")

    # ---- access ------------------------------------------------------------------------------------------------

    def test_http_rules_for_the_stream(self):
        self.start(text("a"))
        self.assertEqual(Console(self.app).post("/api/chat", {"message": "hi"}).status_code, 401)
        self.assertEqual(self.c.post("/api/chat", {"message": "hi"}, origin="https://evil.example").status_code, 403)
        self.assertEqual(self.c.post("/api/chat", {"message": "hi"}, origin=False).status_code, 403)
        r = self.c.post("/api/chat", {"message": "hi"})
        self.assertEqual(r.headers["content-type"].split(";")[0], "text/event-stream")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(self.c.get("/api/chat/status").json()["models"], ["test/model", "test/other"])

    def test_a_locked_session_cannot_use_the_assistant(self):
        self.llm = FakeLlm(text("a"))
        self.cp, self.chat, self.app, self.admin, self.clock = make_app(self.llm)
        c = signed_in(self.cp, self.app, self.admin, unlocked=False)
        r = c.post("/api/chat", {"message": "hi"})
        self.assertEqual((r.status_code, r.json()["error"]), (423, "locked"))
        self.assertEqual(self.llm.calls, [])

    def test_disabled_without_models(self):
        cp, auth, _, admin, *_ = build()
        app = create_app(cp, auth, ApiConfig(origins=(ORIGIN,)), chat=ChatService(cp, FakeLlm(), ChatConfig()))
        c = signed_in(cp, app, admin)
        self.assertEqual(c.post("/api/chat", {"message": "hi"}).status_code, 400)
        self.assertFalse(c.get("/api/chat/status").json()["enabled"])

    def test_without_a_chat_service_there_are_no_routes(self):
        cp, auth, _, admin, *_ = build()
        app = create_app(cp, auth, ApiConfig(origins=(ORIGIN,)))
        c = signed_in(cp, app, admin)
        self.assertEqual(c.post("/api/chat", {"message": "hi"}).status_code, 404)

    def test_conversations_are_private_and_endable(self):
        self.start(text("a"), text("b"))
        conv = self.say("hi")[-1]["conversation_id"]
        other = signed_in(self.cp, self.app, self.admin)
        self.assertIn("ended", self.say("hi", conv, c=other)[0]["message"], "someone else's id looks like a missing one")
        self.assertEqual(other.req("DELETE", f"/api/chat/{conv}").status_code, 400)
        self.assertEqual(self.c.req("DELETE", f"/api/chat/{conv}").status_code, 200)
        self.assertIn("ended", self.say("again", conv)[0]["message"])

    def test_conversations_expire_and_are_capped(self):
        self.start(*[text("a") for _ in range(8)], config=ChatConfig(models=("m",), conversation_ttl=100, max_conversations_per_user=2))
        ids = []
        for i in range(3):
            ids.append(self.say(f"m{i}")[-1]["conversation_id"])
            self.clock.advance(seconds=1)
        self.assertIn("ended", self.say("x", ids[0])[0]["message"], "the oldest was evicted")
        self.assertEqual(self.say("x", ids[2])[-1]["type"], "done")
        self.clock.advance(seconds=200)
        self.assertIn("ended", self.say("x", ids[2])[0]["message"], "idle conversations expire")

    def test_a_model_failure_is_reported_without_provider_text(self):
        self.start(LlmError("the model call failed", 500))
        ev = self.say("hi")
        self.assertEqual(ev, [{"type": "error", "message": "the model call failed"}])
        self.llm.script = [text("recovered")]
        self.assertEqual(self.say("again")[0]["type"], "message", "a failed turn does not wedge the user's slot")


@NEEDS
class OpenRouterTests(unittest.TestCase):
    def client(self, handler, **kw):
        return OpenRouter("sk-secret", client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)

    def test_request_shape_and_privacy_defaults(self):
        seen = {}

        def handler(request):
            seen["req"], seen["body"] = request, json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}], "model": "m", "usage": {"prompt_tokens": 3, "completion_tokens": 4}})
        out = self.client(handler, referer="https://sirosid.dev").complete("m", [{"role": "user", "content": "x"}], [{"type": "function", "function": {"name": "t"}}])
        self.assertEqual(seen["req"].url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(seen["req"].headers["authorization"], "Bearer sk-secret")
        self.assertEqual(seen["body"]["provider"], {"data_collection": "deny"})
        self.assertEqual(seen["body"]["tool_choice"], "auto")
        self.assertEqual(out["usage"], {"prompt_tokens": 3, "completion_tokens": 4})

    def test_errors_never_carry_provider_text_or_the_key(self):
        for status, expect in ((401, "credentials"), (402, "credit"), (429, "rate"), (500, "failed")):
            c = self.client(lambda r, s=status: httpx.Response(s, text="LEAK sk-secret and the prompt"))
            with self.assertRaises(LlmError) as cm:
                c.complete("m", [], [])
            self.assertIn(expect, cm.exception.user_message)
            self.assertNotIn("LEAK", cm.exception.user_message)
            self.assertNotIn("sk-secret", cm.exception.user_message)

    def test_unreadable_and_network_failures(self):
        for handler in (lambda r: httpx.Response(200, text="not json"), lambda r: httpx.Response(200, json={"choices": []})):
            with self.assertRaises(LlmError):
                self.client(handler).complete("m", [], [])

        def boom(request):
            raise httpx.ConnectError("down")
        with self.assertRaises(LlmError):
            self.client(boom).complete("m", [], [])
        with self.assertRaises(ValueError):
            OpenRouter("")


if __name__ == "__main__":
    unittest.main()
