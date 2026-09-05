"""sirosid-dev boot manager - a terminal UI over the harness.

    make install      # once: creates .venv, installs this, launches it
    make manage       # afterwards (or .venv/bin/sirosid-dev)

Every action is a `make` command the developer could have typed (harness.py
builds them and this UI shows them before running), so the TUI can never do
something `make` cannot, and what it does is always reproducible from the
shell. What it adds is the map: every environment in one list, every option
with its help text next to it, the plan before the boot, the pre-flight and
doctor checks as a checklist, and storage as a first-class panel.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Select, Static, Switch

from . import __version__, harness
from .harness import ROOT, Environment, stack


# ---------------------------------------------------------------------------
# Small modals
# ---------------------------------------------------------------------------

class Confirm(ModalScreen[bool]):
    """Yes/no, optionally requiring the user to type a phrase (a wipe)."""

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 70; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    Confirm Label { margin-bottom: 1; }
    Confirm Horizontal { height: auto; align-horizontal: right; }
    Confirm Button { margin-left: 1; }
    """

    def __init__(self, title: str, body: str, phrase: str = "", danger: bool = False):
        super().__init__()
        self.title_text, self.body_text, self.phrase, self.danger = title, body, phrase, danger

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(Text(self.title_text, style="bold"))
            yield Label(self.body_text)
            if self.phrase:
                yield Label(f"Type [b]{self.phrase}[/b] to confirm:")
                yield Input(placeholder=self.phrase, id="phrase")
            with Horizontal():
                yield Button("Cancel", id="no")
                yield Button("Confirm", id="yes", variant="error" if self.danger else "primary")

    @on(Button.Pressed, "#no")
    def cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#yes")
    def confirm(self) -> None:
        if self.phrase and self.query_one("#phrase", Input).value.strip() != self.phrase:
            self.notify("The confirmation text did not match.", severity="error")
            return
        self.dismiss(True)


class AskText(ModalScreen[str | None]):
    DEFAULT_CSS = """
    AskText { align: center middle; }
    AskText > Vertical { width: 70; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    AskText Horizontal { height: auto; align-horizontal: right; }
    AskText Button { margin-left: 1; }
    """

    def __init__(self, title: str, placeholder: str = "", password: bool = False):
        super().__init__()
        self.title_text, self.placeholder, self.password = title, placeholder, password

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(Text(self.title_text, style="bold"))
            yield Input(placeholder=self.placeholder, password=self.password, id="text")
            with Horizontal():
                yield Button("Cancel", id="no")
                yield Button("OK", id="yes", variant="primary")

    @on(Button.Pressed, "#no")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#yes")
    @on(Input.Submitted)
    def ok(self) -> None:
        self.dismiss(self.query_one("#text", Input).value.strip())


# ---------------------------------------------------------------------------
# Command runner: streams a subprocess into a log
# ---------------------------------------------------------------------------

class RunScreen(Screen):
    """Runs one command (a `make` target, `docker compose logs`, `flyctl
    logs`) and streams its output. Escape goes back; the process is killed if
    still running, exactly as Ctrl-C would."""

    BINDINGS = [Binding("escape", "back", "Back"), Binding("ctrl+c", "kill", "Stop", priority=True)]
    DEFAULT_CSS = """
    RunScreen #cmdline { padding: 0 1; background: $primary-background; color: $text; }
    RunScreen RichLog { height: 1fr; }
    RunScreen #status { padding: 0 1; }
    """

    def __init__(self, cmd: list[str], title: str, env: dict | None = None, cwd=ROOT):
        super().__init__()
        self.cmd, self.title_text, self.extra_env, self.cwd = cmd, title, env or {}, cwd
        self.proc: subprocess.Popen | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(f"$ {shlex.join(self.cmd)}", id="cmdline")
        yield RichLog(highlight=False, markup=False, wrap=True, id="log")
        yield Static("running…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.title_text
        self.run_command()

    @work(thread=True, exclusive=True)
    def run_command(self) -> None:
        log = self.query_one("#log", RichLog)
        env = {**os.environ, **self.extra_env}
        try:
            self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=env, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
        except OSError as e:
            self.app.call_from_thread(log.write, Text(f"could not start: {e}", style="bold red"))
            self.app.call_from_thread(self.query_one("#status", Static).update, "failed to start")
            return
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.app.call_from_thread(log.write, Text.from_ansi(line.rstrip("\n")))
        code = self.proc.wait()
        text = "finished OK - Escape to go back" if code == 0 else f"exited with status {code} - Escape to go back"
        self.app.call_from_thread(self.query_one("#status", Static).update,
                                  Text(text, style="bold green" if code == 0 else "bold red"))
        self.app.call_from_thread(self.app.bell) if code else None

    def action_kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.query_one("#status", Static).update("stopped")

    def action_back(self) -> None:
        self.action_kill()
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Options editor: the `local:` block as a form
# ---------------------------------------------------------------------------

class OptionsScreen(Screen):
    """One row per stack option (scripts/stack.py's OPTIONS), with the help
    text that used to live only in Makefile comments. Save writes the
    `local:` block of environments/<name>.yaml; Boot starts the stack with
    these options for this run only."""

    BINDINGS = [Binding("escape", "back", "Back"), Binding("ctrl+s", "save", "Save"), Binding("b", "boot", "Boot with these")]
    DEFAULT_CSS = """
    OptionsScreen VerticalScroll { padding: 0 2; }
    OptionsScreen .opt { height: auto; margin-bottom: 1; }
    OptionsScreen .opt-label { width: 32; padding-top: 1; text-style: bold; }
    OptionsScreen .opt-ctl { width: 30; }
    OptionsScreen .opt-help { width: 1fr; color: $text-muted; padding-top: 1; padding-left: 2; }
    OptionsScreen #plan { height: auto; padding: 1 2; background: $panel; }
    OptionsScreen #actions { height: auto; padding: 1 2; }
    OptionsScreen #actions Button { margin-right: 1; }
    """

    def __init__(self, env: Environment):
        super().__init__()
        self.env = env
        self.values: dict = stack.resolve_options(env.env_arg)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll():
            for opt in stack.OPTIONS:
                if opt.get("transient"):
                    continue
                with Horizontal(classes="opt"):
                    yield Static(f"{opt['label']}\n[dim]{opt['make_var']}=[/dim]", classes="opt-label")
                    key = opt["key"]
                    if opt["type"] == "bool":
                        yield Switch(value=bool(self.values.get(key)), id=f"opt-{key}", classes="opt-ctl")
                    elif opt["type"] == "enum":
                        yield Select([(c, c) for c in opt["choices"]], value=self.values.get(key, opt["default"]),
                                     allow_blank=False, id=f"opt-{key}", classes="opt-ctl")
                    else:
                        yield Input(value=str(self.values.get(key, "")), placeholder="(unset)", id=f"opt-{key}",
                                    classes="opt-ctl")
                    yield Static(opt["help"], classes="opt-help")
        yield Static(id="plan")
        with Horizontal(id="actions"):
            yield Button("Boot with these (this run only)", id="boot", variant="primary")
            yield Button("Save to environments/%s.yaml" % (self.env.name if self.env.env_arg else "…"), id="save")
            yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"options for {self.env.name}"
        self.refresh_plan()

    def current(self) -> dict:
        values = {}
        for opt in stack.OPTIONS:
            if opt.get("transient"):
                continue
            w = self.query_one(f"#opt-{opt['key']}")
            if isinstance(w, Switch):
                values[opt["key"]] = bool(w.value)
            elif isinstance(w, Select):
                values[opt["key"]] = str(w.value) if w.value is not Select.BLANK else opt["default"]
            else:
                values[opt["key"]] = str(w.value).strip()
        return values

    @on(Switch.Changed)
    @on(Select.Changed)
    @on(Input.Changed)
    def refresh_plan(self, _event=None) -> None:
        values = self.current()
        plan = stack.build_plan("", values, with_checks=False)
        lines = [Text("make up " + " ".join(stack.make_vars(plan.options)), style="bold")]
        lines.append(Text("compose: " + ", ".join(f.replace("docker-compose.", "").replace(".yml", "")
                                                     for f in plan.compose_files), style="dim"))
        for s in plan.stores:
            lines.append(Text(f"storage: {s['name']} - {'persistent' if s['persistent'] else 'EPHEMERAL'} "
                              f"({s['volume'] or s['kind']})", style="" if s["persistent"] else "yellow"))
        for w in plan.warnings:
            lines.append(Text("warning: " + w, style="yellow"))
        for e in plan.errors:
            lines.append(Text("error: " + e, style="bold red"))
        self.query_one("#plan", Static).update(Text("\n").join(lines))
        self.query_one("#boot", Button).disabled = bool(plan.errors)

    @on(Button.Pressed, "#back")
    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        if not self.env.env_arg:
            self.app.push_screen(AskText("Name for this environment (creates environments/<name>.yaml)", "alice"),
                                 self._save_as)
            return
        self._save_as(self.env.name)

    def _save_as(self, name: str | None) -> None:
        if not name:
            return
        path = harness.write_local_block(name, self.current())
        self.notify(f"saved local: block to {path.relative_to(ROOT)}")
        self.app.reload_environments()

    @on(Button.Pressed, "#boot")
    def action_boot(self) -> None:
        values = self.current()
        overrides = {k: v for k, v in values.items() if v != stack.OPTION_BY_KEY[k]["default"]}
        self.app.push_screen(RunScreen(harness.up_cmd(self.env, overrides), f"make up ({self.env.name})"))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class StorageScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back"), Binding("r", "refresh", "Refresh"), Binding("c", "clear", "Clear all data")]
    DEFAULT_CSS = """
    StorageScreen #info { height: auto; padding: 1 2; }
    StorageScreen DataTable { height: auto; max-height: 12; margin: 0 2; }
    StorageScreen #actions { height: auto; padding: 1 2; }
    StorageScreen #actions Button { margin-right: 1; }
    StorageScreen RichLog { height: 1fr; margin: 0 2; border: round $panel; }
    """

    def __init__(self, env: Environment, fly: bool):
        super().__init__()
        self.env, self.fly = env, fly
        self.status: dict = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("loading…", id="info")
        yield DataTable(id="dbs")
        with Horizontal(id="actions"):
            yield Button("Clear all data…", id="clear", variant="error")
            yield Button("Refresh", id="refresh")
            yield Button("Back", id="back")
        yield RichLog(id="log", markup=False, highlight=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"storage - {self.env.name} ({'fly' if self.fly else 'local'})"
        t = self.query_one("#dbs", DataTable)
        t.add_columns("database", "size", "collections", "documents")
        self.load()

    @work(thread=True, exclusive=True, group="load")
    def load(self) -> None:
        st = harness.storage_status(self.env, self.fly)
        self.app.call_from_thread(self.render_status, st)

    def render_status(self, st: dict) -> None:
        self.status = st
        info = self.query_one("#info", Static)
        table = self.query_one("#dbs", DataTable)
        table.clear()
        lines = []
        ea = st.get("env_admin")
        if st["target"] == "local":
            for s in st["stores"]:
                style = "" if s["persistent"] else "yellow"
                lines.append(Text(f"{s['name']:<20} {'persistent' if s['persistent'] else 'ephemeral':<11} "
                                  f"{s['volume'] or s['kind']}" + (f"   {s['note']}" if s.get("note") else ""), style=style))
            vols = [v["name"] for v in st["volumes"]]
            lines.append(Text(f"volumes on this machine: {', '.join(vols) if vols else '(none)'}", style="dim"))
        else:
            for v in st.get("volumes", []):
                lines.append(Text(f"{v['app']}: {v['name']} {v['region']} {v['size_gb']} GB "
                                  f"{'attached' if v['attached'] else 'detached'}"))
            if not st.get("volumes"):
                lines.append(Text("no Fly volumes (environment not deployed, or deployed before volumes existed)", style="yellow"))
        if ea:
            lines.append(Text(f"env-admin: up (env={ea['env']}, {'reset in progress' if ea['reset_in_progress'] else 'idle'}, "
                              f"{'can' if ea['control_available'] else 'CANNOT'} restart services)",
                              style="green" if ea["control_available"] else "red"))
            m = ea.get("mongo", {})
            if m.get("reachable"):
                for d in m["databases"]:
                    table.add_row(d["name"] + ("" if d.get("exists") else " (not created)"),
                                  f"{(d.get('size_bytes') or 0) / 1024:.1f} KB", str(d.get("collections", 0)),
                                  str(d.get("documents", 0)))
            else:
                lines.append(Text(f"no Mongo reachable from env-admin ({m.get('error', '')}) - in-memory stores only", style="yellow"))
            if ea.get("last_reset"):
                lr = ea["last_reset"]
                lines.append(Text(f"last reset: {lr['status']} at {time.strftime('%Y-%m-%d %H:%M', time.localtime(lr['started_at']))}"
                                  + (f" dropped {', '.join(lr.get('dropped') or [])}" if lr.get("dropped") else "")))
        else:
            lines.append(Text("env-admin not reachable - environment is down. 'Clear all data' will remove the volumes "
                              "instead (make storage-clear / make fly-storage-clear).", style="yellow"))
        info.update(Text("\n").join(lines))
        self.query_one("#clear", Button).disabled = bool(ea and ea.get("reset_in_progress"))

    @on(Button.Pressed, "#refresh")
    def action_refresh(self) -> None:
        self.load()

    @on(Button.Pressed, "#back")
    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#clear")
    def action_clear(self) -> None:
        name = (self.status.get("env_admin") or {}).get("env") or (self.env.env_arg or "local")
        self.app.push_screen(Confirm(
            "Clear all data",
            f"Every user, passkey and credential in '{name}' is deleted, its services are restarted, and the "
            "issuer and verifier are registered again. Sessions in the wallet will be invalid afterwards.",
            phrase=name, danger=True), self._do_clear)

    def _do_clear(self, ok: bool) -> None:
        if not ok:
            return
        if not self.status.get("env_admin"):
            # Down: volume-level, through make (it asks nothing more - we just confirmed).
            target = "fly-storage-clear" if self.fly else "storage-clear"
            self.app.push_screen(RunScreen(harness.make_cmd(target, self.env, ["YES=yes"]), f"make {target}"))
            return
        token = self.status.get("token") or ""
        if not token:
            self.app.push_screen(AskText("Admin token for this environment (printed by make fly-up)", password=True),
                                 self._run_clear)
        else:
            self._run_clear(token)

    def _run_clear(self, token: str | None) -> None:
        if not token:
            return
        self.query_one("#clear", Button).disabled = True
        self.run_clear(token)

    @work(thread=True, exclusive=True, group="clear")
    def run_clear(self, token: str) -> None:
        log = self.query_one("#log", RichLog)

        def emit(line: str) -> None:
            self.app.call_from_thread(log.write, line)

        status, error = harness.clear_storage(self.env, self.fly, token, emit)
        emit(f"reset {status}" + (f": {error}" if error else ""))
        self.app.call_from_thread(self.load)


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

class DoctorScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back"), Binding("r", "refresh", "Re-run")]
    DEFAULT_CSS = "DoctorScreen DataTable { height: 1fr; margin: 1 2; }"

    def __init__(self, env: Environment | None):
        super().__init__()
        self.env = env

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield DataTable(id="checks")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "doctor"
        self.query_one(DataTable).add_columns("", "check", "detail", "fix")
        self.action_refresh()

    @work(thread=True, exclusive=True)
    def action_refresh(self) -> None:
        checks = harness.doctor(self.env)
        table = self.query_one(DataTable)
        self.app.call_from_thread(table.clear)
        for c in checks:
            mark = Text("skip", style="dim") if c["skipped"] else (Text("ok", style="green") if c["ok"] else Text("FAIL", style="bold red"))
            self.app.call_from_thread(table.add_row, mark, c["name"], c["detail"], c["fix"] if not c["ok"] else "")

    def action_back(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Main screen: environments
# ---------------------------------------------------------------------------

HELP = """\
[b]Environments[/b] - one row per environment: the unnamed local stack, every environments/<name>.yaml,
and every sirosid-<env>-* deployment found on Fly. Select a row; the right side shows what booting it means.

  [b]u[/b] make up (local)        [b]d[/b] make down (local)      [b]o[/b] options editor / plan
  [b]U[/b] make fly-up ENV=       [b]D[/b] make fly-down ENV=     [b]s[/b] / [b]S[/b] storage (local / fly)
  [b]l[/b] / [b]L[/b] logs (local / fly)  [b]e[/b] edit environments/<name>.yaml in $EDITOR
  [b]h[/b] health checks          [b]x[/b] doctor                [b]r[/b] refresh   [b]q[/b] quit

Everything runs as a `make` command shown at the top of the output screen, so it is reproducible from the shell.
"""


class EnvironmentsScreen(Screen):
    BINDINGS = [
        Binding("u", "up", "up"), Binding("d", "down", "down"), Binding("o", "options", "options"),
        Binding("U", "fly_up", "fly-up"), Binding("D", "fly_down", "fly-down"),
        Binding("s", "storage", "storage"), Binding("S", "fly_storage", "fly storage"),
        Binding("l", "logs", "logs"), Binding("L", "fly_logs", "fly logs"), Binding("e", "edit", "edit yaml"),
        Binding("h", "health", "health"), Binding("x", "doctor", "doctor"), Binding("r", "refresh", "refresh"),
        Binding("question_mark", "help", "help"), Binding("q", "quit", "quit"),
    ]
    DEFAULT_CSS = """
    EnvironmentsScreen #body { height: 1fr; }
    EnvironmentsScreen #envs { width: 1fr; }
    EnvironmentsScreen #detail { width: 1fr; padding: 0 1; border-left: solid $panel; }
    EnvironmentsScreen #hint { height: auto; padding: 0 1; color: $text-muted; }
    """

    def __init__(self):
        super().__init__()
        self.envs: list[Environment] = []
        self.selected: Environment | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield DataTable(id="envs", cursor_type="row")
            yield VerticalScroll(Static(id="detail"))
        yield Static("? for help", id="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = str(ROOT)
        t = self.query_one("#envs", DataTable)
        t.add_columns("environment", "file", "local", "fly", "region")
        self.action_refresh()

    @work(thread=True, exclusive=True, group="refresh")
    def action_refresh(self) -> None:
        self.app.call_from_thread(self.query_one("#hint", Static).update, "refreshing…")
        envs = harness.load_environments(include_fly=harness.fly_available())
        local = harness.local_state()
        self.app.call_from_thread(self.render_envs, envs, local)

    def render_envs(self, envs: list[Environment], local_state: str) -> None:
        self.envs = envs
        t = self.query_one("#envs", DataTable)
        t.clear()
        for e in envs:
            t.add_row(e.name, "yes" if e.has_file else ("-" if e.name == "local" else "no"),
                      local_state if e.name == "local" else ("(make up ENV=%s)" % e.name if e.has_file else "-"),
                      "deployed" if e.fly_deployed else "-", e.region or "", key=e.name)
        self.query_one("#hint", Static).update("? for help" + ("" if harness.fly_available() else "   (flyctl not found - Fly columns unavailable)"))
        if envs:
            self.select(self.selected.name if self.selected else envs[0].name)

    def select(self, name: str) -> None:
        self.selected = next((e for e in self.envs if e.name == name), self.envs[0] if self.envs else None)
        if not self.selected:
            return
        e = self.selected
        lines = [Text(e.name, style="bold underline")]
        plan = harness.plan_for(e)
        if e.has_file:
            lines.append(Text(f"environments/{e.name}.yaml", style="dim"))
            fs = e.fly_summary
            lines.append(Text(f"fly: {fs['images']} image pins, {fs['trusted_issuers']} trusted issuers, "
                              f"{fs['trusted_verifiers']} trusted verifiers"
                              + (", conformance" if fs["conformance"] else "")
                              + (f", chart {fs['chart_ref']}" if fs["chart_ref"] else "")))
            if "_error" in e.local_options:
                lines.append(Text(e.local_options["_error"], style="bold red"))
        elif e.name == "local":
            lines.append(Text("the unnamed local docker-compose stack (make up with no ENV=)", style="dim"))
        else:
            lines.append(Text("deployed on Fly with no environments/<name>.yaml (a scratch environment)", style="dim"))
        lines.append(Text(""))
        lines.append(Text("local stack plan", style="bold"))
        for k, v in plan.labels.items():
            if v:
                lines.append(Text(f"  {k + ':':<13}{v}"))
        lines.append(Text("  " + "make up " + " ".join(stack.make_vars(plan.options)), style="dim"))
        lines.append(Text(""))
        lines.append(Text("storage", style="bold"))
        for s in plan.stores:
            lines.append(Text(f"  {s['name']:<20}{'persistent' if s['persistent'] else 'EPHEMERAL':<11} {s['volume'] or s['kind']}",
                              style="" if s["persistent"] else "yellow"))
        lines.append(Text(""))
        lines.append(Text("pre-flight", style="bold"))
        for c in plan.checks:
            lines.append(Text(f"  [{'ok' if c['ok'] else 'FAIL'}] {c['name']}" + ("" if c["ok"] else f" - {c['detail']}"),
                              style="" if c["ok"] else "red"))
        for w in plan.warnings:
            lines.append(Text("warning: " + w, style="yellow"))
        self.query_one("#detail", Static).update(Text("\n").join(lines))

    @on(DataTable.RowHighlighted)
    def row_changed(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            self.select(str(event.row_key.value))

    # -- actions ---------------------------------------------------------

    def _need(self) -> Environment | None:
        if not self.selected:
            self.notify("select an environment first", severity="warning")
        return self.selected

    def _run(self, cmd: list[str], title: str) -> None:
        self.app.push_screen(RunScreen(cmd, title), lambda _r=None: self.action_refresh())

    def action_up(self) -> None:
        if e := self._need():
            plan = harness.plan_for(e)
            if plan.errors:
                self.notify("\n".join(plan.errors), severity="error")
                return
            self._run(harness.up_cmd(e), f"make up ({e.name})")

    def action_down(self) -> None:
        if e := self._need():
            self._run(harness.make_cmd("down", e), "make down")

    def action_options(self) -> None:
        if e := self._need():
            self.app.push_screen(OptionsScreen(e))

    def action_fly_up(self) -> None:
        e = self._need()
        if not e:
            return
        if not e.env_arg:
            self.notify("Fly environments need a name - create one with 'o' then Save, or pick a row with a file",
                        severity="warning")
            return
        body = (f"Deploy sirosid-{e.name}-* to Fly ({'redeploy - it already exists' if e.fly_deployed else 'new environment'}). "
                "Check nobody else is deploying this name right now.")
        self.app.push_screen(Confirm("make fly-up", body),
                             lambda ok: ok and self._run(harness.make_cmd("fly-up", e), f"make fly-up ENV={e.name}"))

    def action_fly_down(self) -> None:
        e = self._need()
        if not e or not e.env_arg:
            return
        self.app.push_screen(Confirm(
            "make fly-down",
            f"Tear down sirosid-{e.name}-*. Confirm to KEEP the Mongo data (KEEP_DATA=yes, volume kept, machines "
            "stopped); cancel here and use the shell for a full teardown that deletes the data too.",
        ), lambda ok: ok and self._run(harness.make_cmd("fly-down", e, ["KEEP_DATA=yes"]), f"make fly-down ENV={e.name} KEEP_DATA=yes"))

    def action_storage(self) -> None:
        if e := self._need():
            self.app.push_screen(StorageScreen(e, fly=False))

    def action_fly_storage(self) -> None:
        if (e := self._need()) and e.env_arg:
            self.app.push_screen(StorageScreen(e, fly=True))

    def action_logs(self) -> None:
        if e := self._need():
            self._run(harness.logs_cmd(e), "docker compose logs")

    def action_fly_logs(self) -> None:
        if (e := self._need()) and e.env_arg:
            self.app.push_screen(AskText("Fly component to show logs for", "wallet-backend"),
                                 lambda c: c is not None and self._run(harness.logs_cmd(e, fly=True, component=c), f"flyctl logs {c}"))

    def action_edit(self) -> None:
        e = self._need()
        if not e:
            return
        if not e.env_arg:
            self.notify("the unnamed local stack has no file - use 'o' and Save to create one", severity="warning")
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        path = e.file
        if not path.exists():
            harness.write_local_block(e.name, {})
        with self.app.suspend():
            subprocess.call([*shlex.split(editor), str(path)])
        self.app.reload_environments()

    def action_health(self) -> None:
        e = self._need()
        if not e:
            return
        self.run_health(e)

    @work(thread=True, exclusive=True, group="health")
    def run_health(self, e: Environment) -> None:
        rows = [(n, harness.health(u)) for n, u in harness.LOCAL_HEALTH]
        if e.env_arg and e.fly_deployed:
            rows += [(f"fly {n}", ok) for n, ok in harness.fly_health(e.name)]
        text = Text("\n").join(Text(f"{'up  ' if ok else 'down'} {n}", style="green" if ok else "red") for n, ok in rows)
        self.app.call_from_thread(self.query_one("#detail", Static).update, text)

    def action_doctor(self) -> None:
        self.app.push_screen(DoctorScreen(self.selected))

    def action_help(self) -> None:
        self.query_one("#detail", Static).update(HELP)


class BootManager(App):
    TITLE = "sirosid-dev"
    CSS = "Screen { layout: vertical; }"

    def on_mount(self) -> None:
        self.push_screen(EnvironmentsScreen())

    def reload_environments(self) -> None:
        for s in self.screen_stack:
            if isinstance(s, EnvironmentsScreen):
                s.action_refresh()


def main() -> None:
    if "--version" in os.sys.argv:
        print(f"sirosid-dev boot manager {__version__} ({ROOT})")
        return
    BootManager().run()
