"""kickstart_site.py against an in-memory GitHub + Cloudflare + rc world (shapes recorded 2026-10-09)."""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "kickstart_site.py"
SPEC = importlib.util.spec_from_file_location("kickstart_site", SCRIPT)
assert SPEC and SPEC.loader
ks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ks)

ACC = ks.DEFAULT_ACCOUNT
TEMPLATE_FILES = {
    "wrangler.jsonc": '{ "name": "__WORKER_NAME__", "previews": {} }\n',
    "package.json": '{ "name": "__WORKER_NAME__" }\n',
    "_data/site.yml": 'name: "__SITE_NAME__"\ncontact_form_action: "https://app.replypen.com/forms/__PROJECT__/__TENANT__"\n',
}
TOKENS = {"CF_SITES_USER_TOKEN": "cf", "GH_SITE_PROVISIONING_PAT": "pat", "CF_BROWSER_RENDERING_TOKEN": "br"}


class World:
    """GitHub + Cloudflare + rc state; every write is appended to `writes`."""

    def __init__(self):
        self.repos = {}  # full_name -> {"id", "files", "sha", "archived"}
        self.installs = {"rootcause-app": (168123503, set()), "cloudflare-workers-and-pages": (169196228, set())}
        self.scripts = {}  # name -> tag
        self.triggers = {}  # uuid -> trigger
        self.builds = []
        self.build_outcomes = []  # consumed per started build; default success
        self.rc_repos = []
        self.writes = []
        self.n = 0

    def uid(self, prefix):
        self.n += 1
        return f"{prefix}-{self.n}"

    # ---- HTTP ----
    def request(self, method, url, *, headers=None, body=None):
        u = urllib.parse.urlsplit(url)
        path, query = u.path, urllib.parse.parse_qs(u.query)
        if method != "GET":
            self.writes.append((method, re.sub(r"^/client/v4/accounts/\w+", "", path), body))
        if u.netloc == "api.github.com":
            return self.github(method, path, query, body)
        if u.netloc == "api.cloudflare.com":
            return self.cloudflare(method, path.removeprefix(f"/client/v4/accounts/{ACC}"), body)
        # the live site: up once the Worker exists and a build succeeded
        if self.scripts and any(b["outcome"] == "success" for b in self.builds):
            return 200, b"<h1>Praktijk Test</h1>"
        return 404, b"not found"

    def ok(self, result, status=200):
        return status, json.dumps({"success": True, "errors": [], "messages": [], "result": result}).encode()

    def github(self, method, path, query, body):
        js = lambda d, s=200: (s, json.dumps(d).encode())
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)", path):
            name = m.group(1)
            if method == "PATCH":
                repo = self.repos.pop(name)
                repo.update(body)
                new = name.split("/")[0] + "/" + body.get("name", name.split("/")[1])
                self.repos[new] = repo
                return js({})
            repo = self.repos.get(name)
            if not repo:
                return js({"message": "Not Found"}, 404)
            return js({"id": repo["id"], "full_name": name, "owner": {"id": 277291192},
                       "html_url": f"https://github.com/{name}"})
        if path == "/repos/rootcause-org/site-template/generate":
            self.repos[f"{body['owner']}/{body['name']}"] = {"id": 1000 + self.n, "files": dict(TEMPLATE_FILES),
                                                              "sha": "a" * 40}
            return js({}, 201)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/contents/(.+)", path):
            text = self.repos[m.group(1)]["files"].get(m.group(2))
            return js({"content": base64.b64encode(text.encode()).decode()}) if text else js({}, 404)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/commits/main", path):
            return js({"sha": self.repos[m.group(1)]["sha"]})
        if re.fullmatch(r"/repos/[^/]+/[^/]+/commits/\w+/check-runs", path):
            done = [b for b in self.builds if b["outcome"]]
            runs = [{"status": "completed", "conclusion": done[-1]["outcome"]}] if done else []
            return js({"check_runs": runs})
        if re.fullmatch(r"/orgs/[^/]+/installations", path):
            return js({"installations": [{"id": i, "app_slug": slug, "repository_selection": "selected"}
                                         for slug, (i, _) in self.installs.items()]})
        if m := re.fullmatch(r"/user/installations/(\d+)/repositories(?:/(\d+))?", path):
            members = next(r for i, r in self.installs.values() if i == int(m.group(1)))
            if method == "PUT":
                members.add(int(m.group(2)))
                return 204, b""
            if method == "DELETE":
                members.discard(int(m.group(2)))
                return 204, b""
            return js({"repositories": [{"id": i} for i in sorted(members)]})
        raise AssertionError(f"unrouted GitHub {method} {path}")

    def cloudflare(self, method, path, body):
        if path.endswith("/browser-rendering/screenshot"):
            return 200, b"\x89PNG fake"
        if path == "/workers/scripts":
            return self.ok([{"id": n, "tag": t} for n, t in self.scripts.items()])
        if m := re.fullmatch(r"/workers/scripts/([\w-]+)", path):
            self.scripts.pop(m.group(1))
            return self.ok(None)
        if path == "/builds/repos/connections":
            if method == "DELETE":
                return self.ok(None)
            return self.ok(body | {"repo_connection_uuid": "conn-1"})
        if path.startswith("/builds/repos/connections/"):
            return self.ok(None)
        if path == "/builds/tokens":
            return self.ok([{"build_token_uuid": "tok-1", "build_token_name": "site-de-kies build token"}])
        if m := re.fullmatch(r"/builds/workers/(\w+)/triggers", path):
            return self.ok([t for t in self.triggers.values() if t["external_script_id"] == m.group(1)])
        if path == "/builds/triggers":
            uuid = self.uid("trig")
            conn = body["repo_connection_uuid"]
            self.triggers[uuid] = {k: v for k, v in body.items() if k != "repo_connection_uuid"} | {
                "trigger_uuid": uuid, "repo_connection": {"repo_connection_uuid": conn}}
            return self.ok(self.triggers[uuid])
        if m := re.fullmatch(r"/builds/triggers/([\w-]+)", path):
            if method == "DELETE":
                self.triggers.pop(m.group(1))
                return self.ok(None)
            self.triggers[m.group(1)].update(body)
            return self.ok(self.triggers[m.group(1)])
        if m := re.fullmatch(r"/builds/triggers/([\w-]+)/builds", path):
            build = {"build_uuid": self.uid("build"), "status": "queued", "outcome": None, "sha": body["commit_hash"],
                     "branch": body["branch"]}
            self.builds.append(build)
            return self.ok({"build_uuid": build["build_uuid"]})
        if re.fullmatch(r"/builds/workers/\w+/builds", path):
            for b in self.builds:  # each poll advances a running build to its end
                if b["status"] != "stopped":
                    b["status"] = "stopped"
                    b["outcome"] = self.build_outcomes.pop(0) if self.build_outcomes else "success"
            return self.ok([{"build_uuid": b["build_uuid"], "status": b["status"], "build_outcome": b["outcome"],
                             "created_on": b["build_uuid"],
                             "build_trigger_metadata": {"commit_hash": b["sha"], "branch": b["branch"]}}
                            for b in self.builds])
        if re.fullmatch(r"/builds/builds/[\w-]+/logs", path):
            return self.ok({"lines": [[0, "boom"]]})
        raise AssertionError(f"unrouted Cloudflare {method} {path}")

    # ---- commands ----
    def run(self, cmd, *, cwd=None, env=None):
        if cmd[:3] == ["gh", "auth", "token"]:
            return "gho_x\n"
        if cmd[:3] == ["gh", "repo", "clone"]:
            target = Path(cmd[4])
            for rel, text in self.repos[cmd[3]]["files"].items():
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                (target / rel).write_text(text)
            target.joinpath(".origin").write_text(cmd[3])
            return ""
        if cmd[0] == "git":
            root = Path(cmd[2])
            if "push" in cmd:
                repo = self.repos[root.joinpath(".origin").read_text()]
                repo["files"] = {rel: (root / rel).read_text() for rel in TEMPLATE_FILES}
                repo["sha"] = "b" * 40
                self.writes.append(("cmd", "git push", None))
            return ""
        if cmd[:2] == ["bash", "build.sh"]:
            return ""
        if cmd[:3] == ["pnpm", "dlx", "wrangler@4"]:
            name = re.search(r'"name": "([^"]+)"', (Path(cwd) / "wrangler.jsonc").read_text()).group(1)
            self.scripts[name] = "tag0"
            self.writes.append(("cmd", "wrangler deploy", None))
            return ""
        if cmd[0] == "rc":
            args = cmd[5:]
            args = args[1:] if args[0] == "project" else args
            verb, params = args[:2], args[2:]
            if verb == ["repo", "ls"]:
                return json.dumps(self.rc_repos)
            self.writes.append(("cmd", "rc " + " ".join(verb), params))
            kv = dict(a.split("=", 1) for a in params if "=" in a)
            if verb == ["repo", "add"]:
                self.rc_repos.append(kv)
            elif verb == ["repo", "set"]:
                next(r for r in self.rc_repos if r["name"] == params[0]).update(kv)
            elif verb == ["repo", "rm"]:
                self.rc_repos = [r for r in self.rc_repos if r["name"] != params[0]]
            return ""
        raise AssertionError(f"unrouted command {cmd}")


class KickstartTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def kick(self, *extra, env=TOKENS):
        args = ks.parse_args(["dentai-org", "test", "--project", "dentai", "--tenant", "test",
                              "--name", "Praktijk Test", "--out", self.tmp.name, *extra])
        k = ks.Kickstart(args, http=self.world, run=self.world.run, sleep=lambda s: None, env=env)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = k.teardown() if args.teardown else k.main()
        self.world.writes, writes = [], self.world.writes
        return code, out.getvalue(), writes

    @staticmethod
    def names(writes):
        return [f"{m} {p}" for m, p, _ in writes]

    def test_fresh_run_provisions_everything_in_order(self):
        code, out, writes = self.kick()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.names(writes), [
            "POST /repos/rootcause-org/site-template/generate",
            "cmd git push",
            "PUT /user/installations/168123503/repositories/1000",
            "PUT /user/installations/169196228/repositories/1000",
            "cmd wrangler deploy",
            "PUT /builds/repos/connections",
            "POST /builds/triggers",
            "POST /builds/triggers",
            "POST /builds/triggers/trig-1/builds",
            "cmd rc repo add",
            "cmd rc dev mirror",
            "POST /browser-rendering/screenshot",
            "POST /browser-rendering/screenshot",
        ])
        files = self.world.repos["dentai-org/site-test"]["files"]
        self.assertIn('"name": "site-test"', files["wrangler.jsonc"])
        self.assertIn("forms/dentai/test", files["_data/site.yml"])
        self.assertIn('name: "Praktijk Test"', files["_data/site.yml"])
        prod, preview = (w[2] for w in writes if w[:2] == ("POST", "/builds/triggers"))
        self.assertEqual((prod["branch_includes"], prod["deploy_command"], prod["build_token_uuid"]),
                         (["main"], "npx wrangler@4 deploy", "tok-1"))
        self.assertEqual((preview["branch_excludes"], preview["deploy_command"]), (["main"], "npx wrangler@4 preview"))
        self.assertTrue(prod["build_caching_enabled"])
        rc_add = next(w[2] for w in writes if w[1] == "rc repo add")
        self.assertIn("preview_url_template=https://{branch}-site-test.rootcause-sites.workers.dev", rc_add)
        self.assertIn("deploy_check=Workers Builds", rc_add)
        self.assertTrue((Path(self.tmp.name) / "test-mobile.png").exists())
        self.assertEqual(len(json.loads((Path(self.tmp.name) / "test-triggers.json").read_text())), 2)
        self.assertIn("✓ screenshots", out)
        self.assertIn("Pas op de website", out)

    def test_rerun_skips_done_steps(self):
        self.kick()
        code, out, writes = self.kick()
        self.assertEqual(code, 0, out)
        # only the idempotent upsert, the mirror refresh and fresh screenshots remain
        self.assertEqual(self.names(writes), [
            "PUT /builds/repos/connections", "cmd rc dev mirror",
            "POST /browser-rendering/screenshot", "POST /browser-rendering/screenshot"])
        self.assertIn("already succeeded", out)

    def test_failed_build_stops_then_rerun_retriggers(self):
        self.world.build_outcomes = ["fail"]
        code, out, writes = self.kick()
        self.assertEqual(code, 1)
        self.assertIn("✗ first build", out)
        self.assertNotIn("cmd rc repo add", self.names(writes))
        self.assertTrue(list(Path(self.tmp.name).glob("test-build-*.log.json")))
        code, out, writes = self.kick()
        self.assertEqual(code, 0, out)
        self.assertIn("POST /builds/triggers/trig-1/builds", self.names(writes))
        self.assertNotIn("POST /builds/triggers", self.names(writes))  # triggers kept

    def test_trigger_drift_is_patched_not_recreated(self):
        self.kick()
        prod = next(t for t in self.world.triggers.values() if t["branch_includes"] == ["main"])
        prod["deploy_command"] = "npx wrangler deploy"
        _, _, writes = self.kick()
        patch = [w for w in writes if w[0] == "PATCH"]
        self.assertEqual(patch, [("PATCH", f"/builds/triggers/{prod['trigger_uuid']}",
                                  {"deploy_command": "npx wrangler@4 deploy"})])

    def test_dry_run_without_credentials_writes_nothing(self):
        world_run = self.world.run
        self.world.run = lambda cmd, **kw: "" if cmd[:3] == ["gh", "auth", "token"] else world_run(cmd, **kw)
        code, out, writes = self.kick("--dry-run", env={})
        self.assertEqual(code, 0, out)
        self.assertEqual(writes, [])
        self.assertIn("(dry-run) would POST /repos/rootcause-org/site-template/generate", out)
        self.assertIn("(dry-run) would $ rc --project dentai --tenant test project repo add name=site", out)
        self.assertIn("result  dry-run", out)

    def test_dry_run_with_credentials_reads_but_never_writes(self):
        self.kick()
        code, out, writes = self.kick("--dry-run")
        self.assertEqual((code, writes), (0, []))
        self.assertIn("(dry-run) would PUT", out)  # the connection upsert is still only planned

    def test_mirror_name_collision_is_refused(self):
        self.world.rc_repos = [{"name": "site", "git_url": "https://github.com/x/other.git"}]
        code, out, _ = self.kick()
        self.assertEqual(code, 1)
        self.assertIn("--mirror-name", out)

    def test_teardown_undoes_and_frees_the_name(self):
        self.kick()
        code, out, _ = self.kick("--teardown", "wrong-name")
        self.assertEqual(code, 1)
        code, out, _ = self.kick("--teardown", "site-test")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.world.scripts, self.world.triggers, self.world.rc_repos), ({}, {}, []))
        self.assertTrue(all(not members for _, members in self.world.installs.values()))
        parked = [n for n in self.world.repos if n.startswith("dentai-org/site-test-teardown-")]
        self.assertEqual(len(parked), 1)
        self.assertTrue(self.world.repos[parked[0]]["archived"])
        self.assertIn("gh repo delete", out)
        code, out, _ = self.kick()  # the slug is free again
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
