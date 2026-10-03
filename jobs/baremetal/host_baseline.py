"""
Nautobot Job: Host Baseline (SoT-driven) — the L1/L2 baseline of an
installed Proxmox node (decisions #54/#55; design: docs/host-baseline.md;
SoT model: docs/sot-data-contract.md §4c; runbook: docs/baremetal-install.md).

What the tester's hand-run post-deploy script did, driven from Nautobot:
packages, SNMP (snmpd.conf + SNMPv3 users), the AD realm with its sync job,
admin-group ACL and the root e-mail, service accounts with API tokens
captured into per-node SecretsGroups, and — LAST, under a rollback timer —
the bond/bridge network rendered from the Device's interfaces.

Fail closed: every SoT fact the run needs is checked first (named refusals,
all at once); then the node's identity (hostname + DMI serial = the Device);
then a read-only observation of the node and a plan (a member NIC that is
not on the node, an unknown PVE role, a conflicting realm refuse here) —
all before the first write. A dry run renders and diffs everything and
writes nothing (drift detection).

Transport: SSH as root with the host_ssh_username / host_ssh_password
Secrets (as the host-verification job). The applier
(jobs/lib/host_baseline_applier.sh) and each step's inputs travel over the
session's stdin; nothing is written to the node's disk except the managed
files themselves, and no secret is ever on an argv or in a log line.
"""

import os
import time
from datetime import datetime, timezone

from nautobot.apps.jobs import BooleanVar, Job, ObjectVar, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.models import Secret
from nautobot.ipam.models import IPAddress

from ..lib import host_baseline as hb
from ..lib.answer_service import NFV_ROLE, nfv_role_refusal
from ..lib.nautobot_helpers import NODE_SECRETS_DIR, store_node_token, stored_node_token

STEPS = (
    "Gates (SoT facts)",
    "Identity (hostname + DMI serial)",
    "Packages",
    "SNMP",
    "AD realm + root e-mail",
    "Service accounts",
    "Network (bond/bridge, rollback timer)",
    "provisioning_state",
)


class StepFailed(RuntimeError):
    """A step reported a failure on the node — later steps (the network
    above all) do not run."""


class HostBaseline(Job):
    class Meta:
        name = "Host Baseline (SoT-driven)"
        description = (
            "Baselines an installed NFV-role Proxmox node from the SoT over root SSH: packages, "
            "SNMP, AD realm + sync job + admin ACL, root e-mail, service-account tokens (stored "
            "as per-node SecretsGroups), and last the bond/bridge network under a rollback timer. "
            "Dry run (default) renders and diffs everything and writes nothing."
        )
        has_sensitive_variables = False
        # apt (~10 min worst case) + the network apply with its rollback
        # window (<= 900 s) + reconnects; dry runs take a minute.
        soft_time_limit = 2400
        time_limit = 2700

    device = ObjectVar(
        model=Device,
        label="Node",
        description=(
            "Installed NFV-role Device (provisioning_state bm_installed or baseline_done; a dry "
            "run accepts any installed state)"
        ),
        query_params={"role": NFV_ROLE},
    )
    dry_run = BooleanVar(
        label="Dry run (drift report)",
        description="Observe the node, render everything from the SoT, report each difference — write nothing.",
        default=True,
    )
    confirm = BooleanVar(
        label="Confirm changes",
        description=(
            "Required when dry run is off: changes packages, SNMP, the AD realm, service accounts "
            "and — last, under a rollback timer — the node's network."
        ),
        default=False,
    )

    # ------------------------------------------------------------ plumbing

    def _scrub(self, text):
        return hb.scrub(text, self._secret_values)

    def _log(self, level, message, *args):
        getattr(self.logger, level)("%s", self._scrub(message % args if args else message))

    def _step_line(self, number, status, detail):
        line = f"[{number}/{len(STEPS)}] {STEPS[number - 1]}: {status} — {detail}"
        self._summary.append(self._scrub(line))
        level = {"failed": "error", "refused": "error", "warning": "warning"}.get(status, "info")
        self._log(level, line)

    def _secret(self, name, purpose, device, problems, sensitive=True):
        try:
            value = Secret.objects.get(name=name).get_value(obj=device)
        except Secret.DoesNotExist:
            problems.append(
                f"Secret {name!r} ({purpose}) does not exist — re-run Bootstrap NFV Data Model (it "
                f"pre-creates the record) and supply the value (composer: ./add-secret.sh {name})"
            )
            return None
        except Exception as exc:  # SecretError & co — the record has no readable value
            problems.append(
                f"Secret {name!r} ({purpose}) has no readable value ({type(exc).__name__}) — supply it "
                f"(composer: ./add-secret.sh {name})"
            )
            return None
        value = "" if value is None else str(value)
        problem = hb.secret_value_problem(purpose, name, value)
        if problem:
            problems.append(problem)
            return None
        if sensitive:  # scrubbed from every log line (a login name like "root" is not)
            self._secret_values.add(value)
        return value

    def _connect(self, host, timeout=15):
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "paramiko is not installed in this worker — it ships with the composer stack's "
                "device-onboarding/Nornir dependencies"
            ) from exc
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # lab tooling, as verify_host
        client.connect(host, username=self._ssh_user, password=self._ssh_password, timeout=timeout,
                       banner_timeout=timeout, auth_timeout=timeout, look_for_keys=False,
                       allow_agent=False)
        return client

    def _apply(self, client, locals_, calls, timeout, require_done=True):
        """Run one payload over `bash -s` -> events (partial ones when the
        session drops). Token values are moved out of the events into
        self._token_values the moment they are parsed, so no event dict ever
        carries one. With require_done, an unfinished applier (no 'done'
        event) raises StepFailed."""
        payload = hb.build_payload(self._applier, locals_, calls)
        out_chunks, err, rc = [], "", None
        try:
            stdin, stdout, stderr = client.exec_command("bash -s", timeout=timeout)
            stdin.write(payload)
            stdin.flush()
            stdin.channel.shutdown_write()
            while True:
                chunk = stdout.read(65536)
                if not chunk:
                    break
                out_chunks.append(chunk)
            err = stderr.read().decode("utf-8", errors="replace")
            rc = stdout.channel.recv_exit_status()
        except Exception as exc:  # timeout / dropped session: keep what arrived
            self._session_error = type(exc).__name__
            if require_done:
                events, _ = hb.parse_events(b"".join(out_chunks).decode("utf-8", errors="replace"))
                self._take_token_values(events)
                raise StepFailed(f"the SSH session to the node ended mid-step ({type(exc).__name__})") from exc
        events, noise = hb.parse_events(b"".join(out_chunks).decode("utf-8", errors="replace"))
        self._take_token_values(events)
        if noise:
            self.logger.debug("applier printed %d unrecognised stdout line(s) (not logged)", noise)
        if require_done and not any(e.get("step") == "done" for e in events):
            failed = [e for e in events if e.get("status") == "failed"]
            detail = failed[0].get("detail") if failed else (err.strip().splitlines() or ["no output"])[-1]
            raise StepFailed(self._scrub(f"the applier did not finish (rc={rc}): {detail}"))
        return events

    def _take_token_values(self, events):
        for event in events:
            if event.get("event") == "token":
                value = event.pop("value", None)
                if isinstance(value, str) and value:
                    self._secret_values.add(value)
                    self._token_values[str(event.get("tokenid"))] = value

    def _report(self, number, events):
        """Log a step's events; -> (status, detail) for its result line."""
        counts = {}
        for event in events:
            if event.get("step") in ("observe", "done"):
                continue
            status = event.get("status", "info")
            counts[status] = counts.get(status, 0) + 1
            level = {"failed": "error", "warning": "warning"}.get(status, "info")
            self._log(level, "[%d/%d] %s: %s — %s", number, len(STEPS), event.get("item"), status,
                      event.get("detail", ""))
            if event.get("diff_b64"):
                import base64

                diff = base64.b64decode(event["diff_b64"]).decode("utf-8", errors="replace")
                self._log("info", "%s", diff or "(only secret-bearing values differ — masked)")
        if counts.get("failed"):
            status = "failed"
        elif counts.get("would_change"):
            status = "would change"
        elif counts.get("changed"):
            status = "changed"
        elif counts.get("warning"):
            status = "warning"
        else:
            status = "ok"
        detail = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "nothing to do"
        return status, detail

    def _run_step(self, number, client, locals_, calls, timeout=300):
        events = self._apply(client, [("DRY_RUN", "1" if self._dry else "0")] + list(locals_), calls,
                             timeout)
        status, detail = self._report(number, events)
        self._step_line(number, status, detail)
        if status == "failed" and not self._dry:
            raise StepFailed(f"{STEPS[number - 1]} failed on the node — see the log; later steps "
                             "(the network above all) did not run")
        return events

    # ---------------------------------------------------------- 1. gates

    def _gates(self, device, dry_run, confirm):
        refusal = nfv_role_refusal(device, "baseline it")
        if refusal:
            raise hb.BaselineRefusal([refusal])
        if not dry_run and not confirm:
            raise hb.BaselineRefusal(["Dry run is off but Confirm is not ticked — refusing to change the node"])
        problems = []
        state = device.cf.get("provisioning_state")
        allowed = hb.DRY_RUN_STATES if dry_run else hb.APPLY_STATES
        if state not in allowed:
            problems.append(
                f"{device.name} provisioning_state is {state!r} — the Host Baseline applies to "
                f"{' / '.join(hb.APPLY_STATES)} nodes (a dry run to any installed state: "
                f"{', '.join(hb.DRY_RUN_STATES)}); set the state in the SoT first"
            )
        if not device.serial:
            problems.append(f"{device.name} has no serial — the identity check compares it with the node's DMI serial")
        if device.primary_ip4 is None:
            problems.append(f"{device.name} has no primary_ip4 — the job connects to it and renders it "
                            "onto the management bridge")
        cfg, cfg_problems = hb.validate_context(device.get_config_context())
        problems += cfg_problems
        ctx = {"cfg": cfg}
        self._ssh_user = self._secret(hb.HOST_SSH_USERNAME_SECRET, "root SSH login", device, problems,
                                      sensitive=False)
        self._ssh_password = self._secret(hb.HOST_SSH_PASSWORD_SECRET, "root SSH password", device, problems)
        if cfg is None:
            raise hb.BaselineRefusal(problems)
        secrets = {}
        for purpose, name in hb.secret_names(cfg):
            secrets[(purpose, name)] = self._secret(name, purpose, device, problems)
        ctx["secrets"] = secrets
        if cfg["snmp"]:
            location, problem = hb.snmp_location(getattr(device.location, "name", None))
            if problem:
                problems.append(problem)
            ctx["sys_location"] = location
        if cfg["service_accounts"] and not dry_run and not os.access(NODE_SECRETS_DIR, os.W_OK):
            problems.append(
                f"the worker cannot write {NODE_SECRETS_DIR} — service-account tokens are stored there "
                "as text-file Secrets (the answer service's mechanism); mount secrets/nodes read-write "
                "into the Celery worker (nautobot-composer host-baseline-support) or point "
                "NFV_NODE_SECRETS_DIR at a writable directory both Nautobot containers see"
            )
        if cfg["network"] is not None and device.primary_ip4 is not None:
            interfaces = self._interface_records(device)
            address, ids, gateway, gw_problem = self._primary(device)
            if gw_problem:
                problems.append(f"{device.name} network model: {gw_problem}")
            model, net_problems = hb.build_network_model(device.name, interfaces, address, ids,
                                                         gateway, cfg["network"])
            problems += net_problems
            ctx["network_model"] = model
        if problems:
            raise hb.BaselineRefusal(problems)
        return ctx

    def _interface_records(self, device):
        records = []
        for iface in device.interfaces.all().prefetch_related("tagged_vlans", "ip_addresses"):
            records.append(hb.interface_record(
                id=iface.pk, name=iface.name, type=iface.type, lag=iface.lag_id,
                bridge=iface.bridge_id, mac=iface.mac_address, mtu=iface.mtu, mode=iface.mode,
                tagged_vids=[vlan.vid for vlan in iface.tagged_vlans.all()],
                description=iface.description, custom_fields=iface.cf,
                ips=[str(ip.address) for ip in iface.ip_addresses.all()],
            ))
        return records

    def _primary(self, device):
        primary = device.primary_ip4
        ids = [str(i.pk) for i in primary.interfaces.filter(device=device)]
        gateways = []
        if getattr(primary, "parent", None) is not None:
            gateways = [str(ip.address) for ip in
                        IPAddress.objects.filter(parent=primary.parent, role__name="DefaultGW")]
        gateway, problem = hb.pick_gateway(gateways)
        return str(primary.address), ids, gateway, problem

    # ---------------------------------------------- 2. identity + observe

    def _observe(self, client, cfg):
        realm = (cfg.get("ad") or {}).get("realm") or ""
        events = self._apply(client, [("DRY_RUN", "1"), ("OBS_PKGS", cfg["packages"]),
                                      ("OBS_REALM", realm)], [["nfv_step_observe"]], timeout=120)
        return hb.decode_observation(events)

    # -------------------------------------------------------- planning

    def _plan(self, device, ctx, obs):
        """Everything decided from the observation, before the first write;
        node facts that contradict the SoT refuse here."""
        cfg, problems, plan = ctx["cfg"], [], {}
        roles = {r.get("roleid") for r in hb.json_from(obs, "pve.roles", []) or () if isinstance(r, dict)}
        wanted_roles = {a["role"] for a in cfg["service_accounts"]}
        if cfg["ad"]:
            wanted_roles.add(cfg["ad"]["admin_role"])
        if hb.json_from(obs, "pve.roles") is None:
            problems.append("could not list the node's PVE roles (pveum role list) — is this a Proxmox VE node?")
        else:
            for role in sorted(wanted_roles - roles):
                problems.append(f"PVE role {role!r} (named in host_baseline) does not exist on the node — "
                                "use a built-in role (e.g. PVEAuditor, Administrator) or create it first")
        users = hb.json_from(obs, "pve.users", []) or []
        acls = hb.json_from(obs, "pve.acls", []) or []
        if cfg["ad"]:
            ad = cfg["ad"]
            current = hb.json_from(obs, "pve.realm") if obs.get("pve.realm", {}).get("rc") == 0 else None
            realm_plan = hb.plan_realm(ad, current)
            sync_plan = hb.plan_sync_job(ad, hb.json_from(obs, "pve.sync_jobs", []))
            for p in (realm_plan, sync_plan):
                if p["action"] == "refuse":
                    problems.append(p["problem"])
            plan["realm"], plan["sync"], plan["realm_exists"] = realm_plan, sync_plan, current is not None
        plan["root_email"] = hb.plan_root_email(cfg["root_email"], users)
        stored = {}
        host = str(device.primary_ip4.address.ip)
        for account in cfg["service_accounts"]:
            token_id, value = stored_node_token(device, account["name"])
            if value:
                self._secret_values.add(value)
            if token_id != hb.token_full_id(account) or not hb.valid_token_value(value):
                stored[account["name"]] = "missing"
                continue
            verdict = self._verify_token(host, token_id, value)
            if verdict == "unreachable":
                self._log("warning", "Could not verify the stored %s token against https://%s:8006 from "
                          "the worker — keeping it (it is re-checked on the next run)", token_id, host)
            stored[account["name"]] = "invalid" if verdict == "invalid" else "ok"
        plan["accounts"] = hb.plan_service_accounts(cfg["service_accounts"], users, acls, stored)
        model = ctx.get("network_model")
        if model is not None:
            links = hb.parse_ip_link(obs.get("net.links", {}).get("text", ""))
            physical = (obs.get("net.physical", {}).get("text") or "").split()
            names, match_problems = hb.match_ports(model, links, physical)
            problems += [f"{device.name} network: {p}" for p in match_problems]
            plan["linux_names"] = names
            if not match_problems:
                plan["iface_render"] = hb.render_interfaces(model, names)
                plan["iface_current"] = obs.get("net.interfaces", {}).get("text", "")
                if not plan["iface_current"].endswith("\n") and plan["iface_current"]:
                    plan["iface_current"] += "\n"  # $(...) in the applier strips the final newline
        if problems:
            raise hb.BaselineRefusal(problems)
        return plan

    def _verify_token(self, host, token_id, value):
        """-> ok | invalid | unreachable (GET /version needs only a valid token)."""
        try:
            import requests
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            response = requests.get(
                f"https://{host}:8006/api2/json/version",
                headers={"Authorization": f"PVEAPIToken={token_id}={value}"},
                verify=False, timeout=10,
            )
        except Exception:
            return "unreachable"
        if response.status_code == 200:
            return "ok"
        if response.status_code == 401:
            return "invalid"
        return "unreachable"

    # ------------------------------------------------------- 3-6 steps

    def _snmp_locals(self, device, ctx):
        snmp, secrets = ctx["cfg"]["snmp"], ctx["secrets"]
        community = secrets.get(("SNMP community", snmp.get("community_secret"))) if snmp.get("community_secret") else None
        conf = hb.render_snmpd_conf(
            location=ctx["sys_location"], contact=snmp["contact"], community=community,
            community_source=snmp["community_source"], community_view=snmp["community_view"],
            v3_users=snmp["v3_users"],
        )
        names, auth_p, auth, priv_p, priv, fps = [], [], [], [], [], []
        for user in snmp["v3_users"]:
            a = secrets[(f"SNMPv3 {user['name']} auth passphrase", user["auth_secret"])]
            p = secrets[(f"SNMPv3 {user['name']} priv passphrase", user["priv_secret"])]
            names.append(user["name"])
            auth_p.append(user["auth_protocol"])
            auth.append(a)
            priv_p.append(user["priv_protocol"])
            priv.append(p)
            fps.append(hb.snmpv3_fingerprint(str(device.pk), user["name"], user["auth_protocol"], a,
                                             user["priv_protocol"], p))
        return [("SNMPD_CONF_CONTENT", conf), ("V3_NAMES", names), ("V3_AUTH_PROTO", auth_p),
                ("V3_AUTH_PASS", auth), ("V3_PRIV_PROTO", priv_p), ("V3_PRIV_PASS", priv), ("V3_FP", fps)]

    def _ad_calls(self, ctx, plan):
        cfg, calls, locals_ = ctx["cfg"], [], []
        ad = cfg["ad"]
        if ad:
            locals_.append(("REALM_BIND_PASSWORD", ctx["secrets"][("AD bind password", ad["bind_password_secret"])]))
            realm_plan = plan["realm"]
            if realm_plan["action"] in ("add", "modify"):
                calls.append(["nfv_run", "ad", f"realm {ad['realm']} ({realm_plan['action']}: "
                              f"{', '.join(realm_plan['changes'])})", *realm_plan["argv"]])
            else:
                calls.append(["nfv_emit", "ad", f"realm {ad['realm']}", "ok", "realm options match the SoT"])
            calls.append(["nfv_realm_password", "ad", ad["realm"]])
            sync_plan = plan["sync"]
            if sync_plan["action"] in ("create", "set"):
                calls.append(["nfv_run", "ad", f"sync job {ad['sync_job']['name']} ({sync_plan['action']}: "
                              f"{', '.join(sync_plan['changes'])})", *sync_plan["argv"]])
            else:
                calls.append(["nfv_emit", "ad", f"sync job {ad['sync_job']['name']}", "ok",
                              f"scheduled {ad['sync_job']['schedule']} as the SoT says"])
            if self._dry and plan["realm_exists"]:
                calls.append(["nfv_realm_probe", "ad", *hb.realm_sync_argv(ad, dry_run=True)])
            force = "1" if realm_plan["action"] in ("add", "modify") else "0"
            calls.append(["nfv_realm_sync", "ad", force, ad["admin_group"], *hb.realm_sync_argv(ad)])
            calls.append(["nfv_group_acl", "ad", ad["admin_path"], ad["admin_group"], ad["admin_role"]])
        else:
            calls.append(["nfv_emit", "ad", "realm", "skipped", "host_baseline.ad.enabled is false"])
        if plan["root_email"]:
            calls.append(["nfv_run", "ad", "root@pam e-mail", *plan["root_email"]])
        else:
            calls.append(["nfv_emit", "ad", "root@pam e-mail", "ok", f"already {cfg['root_email']}"])
        return locals_, calls

    def _account_calls(self, plan):
        calls = []
        for p in plan["accounts"]:
            acc = p["account"]
            full = hb.token_full_id(acc)
            if p["user_add"]:
                calls.append(["nfv_run", "accounts", f"user {acc['user']}", *p["user_add"]])
            if p["token"] == "keep":
                calls.append(["nfv_emit", "accounts", f"token {full}", "ok", p["token_reason"]])
            else:
                calls.append(["nfv_token", "accounts", acc["name"], acc["user"], acc["token"],
                              "1" if acc["privsep"] else "0", p["token"], p["token_reason"]])
            if p["privsep_fix"]:
                calls.append(["nfv_run", "accounts", f"token {full} privsep", *p["privsep_fix"]])
            for argv in p["acl_deletes"]:
                calls.append(["nfv_run", "accounts", f"acl {argv[3]} {argv[5]} -{argv[7]}", *argv])
            for argv in p["acl_adds"]:
                calls.append(["nfv_run", "accounts", f"acl {argv[3]} {argv[5]} +{argv[7]}", *argv])
            if not (p["acl_adds"] or p["acl_deletes"]):
                calls.append(["nfv_emit", "accounts", f"acl {acc['user']}", "ok",
                              f"{acc['role']} on {acc['path']} as the SoT says"])
        return calls

    def _store_tokens(self, device, events, host):
        """Token events -> Secrets (the phone-home mechanism). The values were
        moved out of the events by _apply; nothing here logs one."""
        stored = []
        for event in events:
            if event.get("event") != "token":
                continue
            token_id = str(event.get("tokenid"))
            value = self._token_values.pop(token_id, None)
            if not hb.valid_token_value(value):
                raise StepFailed(f"the node returned no usable value for {token_id} — re-run (a token whose "
                                 "Secrets are missing is rotated)")
            group = store_node_token(device, event["account"], token_id, value)
            verdict = self._verify_token(host, token_id, value)
            if verdict == "invalid":
                raise StepFailed(f"the new token {token_id} was stored in SecretsGroup {group!r} but the node "
                                 "rejects it (401) — re-run")
            note = "verified against the node API" if verdict == "ok" else "not verified (worker cannot reach :8006)"
            self._log("info", "[6/%d] token %s stored in SecretsGroup %r (%s)", len(STEPS), token_id, group, note)
            stored.append(group)
        return stored

    # ---------------------------------------------------------- 7. network

    def _bond_check(self, ctx, plan, bonding_text, fail_on_errors):
        model = ctx["network_model"]
        names = plan.get("linux_names") or {}
        dump = hb.split_bonding_dump(bonding_text)
        errors, warnings = [], []
        for bond in model["bonds"]:
            members = [names.get(m, model["names"][m]) for m in bond["members"]]
            primary = names.get(bond["primary"]) if bond.get("primary") else None
            e, w = hb.evaluate_bond(bond, members, primary, dump.get(bond["name"]))
            errors += e
            warnings += w
        for warning in warnings:
            self._log("warning", "[7/%d] bond state: %s", len(STEPS), warning)
        for error in errors:
            self._log("error" if fail_on_errors else "warning", "[7/%d] bond state: %s", len(STEPS), error)
        return errors, warnings

    def _network(self, device, ctx, plan, client, obs):
        cfg = ctx["cfg"]
        if cfg["network"] is None:
            self._step_line(7, "skipped", "host_baseline.network.enabled is false")
            return client
        render, current = plan["iface_render"], plan["iface_current"]
        diff = hb.unified_diff(current, render, "/etc/network/interfaces")
        if diff:
            self._log("info", "[7/%d] /etc/network/interfaces differs from the SoT render:\n%s", len(STEPS), diff)
        if obs.get("net.pending", {}).get("text", "").strip() == "present":
            self._log("warning", "[7/%d] pending PVE GUI network changes exist in /etc/network/interfaces.new "
                      "— a non-dry run discards them", len(STEPS))
        if self._dry or not diff:
            errors, warnings = self._bond_check(ctx, plan, obs.get("net.bonding", {}).get("text", ""),
                                                fail_on_errors=not self._dry)
            if not diff:
                status = "failed" if errors and not self._dry else ("warning" if errors or warnings else "ok")
                self._step_line(7, status, "in sync with the SoT render"
                                + (f"; {len(errors)} bond error(s)" if errors else "")
                                + (f"; {len(warnings)} bond warning(s)" if warnings else ""))
                if errors and not self._dry:
                    raise StepFailed("the running bonds do not match the SoT although the file does — "
                                     "see the bond-state errors (ifreload -a by hand, or check the NICs)")
            else:
                self._step_line(7, "would change", "diff above; dry run — nothing staged (running bond "
                                f"state: {len(errors)} error(s), {len(warnings)} warning(s) vs the SoT)")
            return client
        return self._network_apply(device, cfg, ctx, plan, client, render)

    def _network_apply(self, device, cfg, ctx, plan, client, render):
        host = str(device.primary_ip4.address.ip)
        rollback = cfg["network"]["rollback_seconds"]
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        unit = f"nfv-baseline-net-rollback-{ts}"
        marker = f"/run/nfv-baseline/net-rollback-{ts}.done"
        start = time.monotonic()
        self._session_error = None
        events = self._apply(client, [("DRY_RUN", "0"), ("NEW_IFACES", render),
                                      ("ROLLBACK_SECONDS", str(rollback)), ("NET_TS", ts)],
                             [["nfv_step_network_apply"]], timeout=max(30, rollback // 3),
                             require_done=False)
        net = {e.get("item"): e for e in events if e.get("step") == "network"}
        if any(e.get("status") == "failed" for e in net.values()):
            status, detail = self._report(7, events)
            self._step_line(7, "failed", detail)
            raise StepFailed("the network apply failed on the node; the applier left (or restored) the "
                             "previous configuration and cancelled its timer — see the log")
        if net.get("apply", {}).get("status") == "ok":
            self._report(7, events)
            self._step_line(7, "ok", "the node already matched the render")
            return client
        self._report(7, events)
        if self._session_error or "apply" not in net:
            self._log("warning", "[7/%d] the SSH session ended during the apply (%s) — expected when the "
                      "management path moves; reconnecting on %s", len(STEPS),
                      self._session_error or "no result", host)
        try:
            client.close()
        except Exception:
            pass
        deadline = start + rollback - 20
        fresh = None
        while time.monotonic() < deadline:
            try:
                fresh = self._connect(host, timeout=10)
                self._clients.append(fresh)
                break
            except Exception:
                time.sleep(5)
        if fresh is None:
            time.sleep(max(0, start + rollback + 45 - time.monotonic()))
            try:
                self._connect(host, timeout=10).close()
                verdict = "the node answers again on the previous configuration"
            except Exception:
                verdict = ("the node is still unreachable after the rollback window — console access "
                           "(XCC / serial) needed")
            self._step_line(7, "failed", f"no SSH on {host} within {rollback - 20}s after the apply")
            raise StepFailed(
                f"network apply lost management reachability: no SSH on {host} within {rollback - 20}s, so "
                f"the rollback timer ({unit}) restores the previous /etc/network/interfaces — {verdict}. "
                "Fix the SoT model (members, modes, MTU) or the switch side, then re-run"
            )
        events = self._apply(fresh, [("DRY_RUN", "0"), ("NET_UNIT", unit), ("NET_MARKER", marker)],
                             [["nfv_step_network_confirm"]], timeout=60)
        status, detail = self._report(7, events)
        if status == "failed":
            self._step_line(7, "failed", detail)
            raise StepFailed("the rollback timer fired before the job could cancel it — the previous network "
                             "configuration is back; re-run (raise host_baseline.network.rollback_seconds if "
                             "the reconnect is slow)")
        state = hb.decode_observation(self._apply(fresh, [("DRY_RUN", "1")], [["nfv_step_network_state"]],
                                                  timeout=60))
        running = state.get("net.interfaces", {}).get("text", "")
        if running.rstrip("\n") != render.rstrip("\n"):
            self._step_line(7, "failed", "the node's /etc/network/interfaces is not the render after the apply")
            raise StepFailed("the apply did not take effect (the session ended before the file was moved into "
                             "place?) — the node keeps its previous configuration; re-run (a leftover "
                             "/etc/network/interfaces.new is replaced by the next run)")
        errors, warnings = self._bond_check(ctx, plan, state.get("net.bonding", {}).get("text", ""),
                                            fail_on_errors=True)
        if errors:
            self._step_line(7, "failed", f"applied and confirmed, but {len(errors)} bond error(s)")
            raise StepFailed("the network was applied and management reconnected, but the running bonds "
                             "do not match the SoT — see the bond-state errors")
        self._step_line(7, "warning" if warnings else "changed",
                        f"applied; reconnected on {host}; rollback timer cancelled"
                        + (f"; {len(warnings)} bond warning(s) (LACP/link — switch side)" if warnings else ""))
        return fresh

    # -------------------------------------------------------------- run

    def _refuse(self, number, exc):
        """Log every problem on its own line, then re-raise (job fails)."""
        self._step_line(number, "refused", f"{len(exc.problems)} problem(s) — nothing was written")
        for problem in exc.problems:
            self._log("error", "  refused: %s", problem)
        raise exc

    def _open(self, host):
        try:
            client = self._connect(host)
        except Exception as exc:
            raise RuntimeError(
                f"could not open an SSH session to {host} as the {hb.HOST_SSH_USERNAME_SECRET} login "
                f"({type(exc).__name__}: {self._scrub(exc)}) — check primary_ip4, root SSH (PermitRootLogin) "
                f"and the {hb.HOST_SSH_USERNAME_SECRET}/{hb.HOST_SSH_PASSWORD_SECRET} Secrets"
            ) from None
        self._clients.append(client)
        return client

    def run(self, device, dry_run, confirm):
        self._secret_values = set()
        self._token_values = {}
        self._session_error = None
        self._summary = []
        self._clients = []
        self._dry = bool(dry_run)
        self._applier = hb.APPLIER_PATH.read_text()
        try:
            ctx = self._gates(device, dry_run, confirm)
        except hb.BaselineRefusal as exc:
            self._refuse(1, exc)
        cfg = ctx["cfg"]
        self._step_line(1, "ok", f"role NFV, state {device.cf.get('provisioning_state')}, config context "
                        f"host_baseline complete, {len(ctx['secrets'])} Secret(s) resolved"
                        + (", network model valid" if ctx.get("network_model") else ", network step disabled"))
        host = str(device.primary_ip4.address.ip)
        try:
            client = self._open(host)
            obs = self._observe(client, cfg)
            ident = hb.identity_problems(device.name, device.serial,
                                         obs.get("identity.hostname", {}).get("text"),
                                         obs.get("identity.serial", {}).get("text"))
            if (obs.get("identity.uid", {}).get("text") or "").strip() != "0":
                ident.append(f"the {hb.HOST_SSH_USERNAME_SECRET} login is not root — the baseline writes "
                             "root-only files and never uses sudo")
            if ident:
                self._refuse(2, hb.BaselineRefusal(ident))
            self._step_line(2, "ok", f"{host} is {device.name} (hostname and DMI serial match); "
                            f"{(obs.get('pve.version', {}).get('text') or 'pve ?').strip()}")
            try:
                plan = self._plan(device, ctx, obs)
            except hb.BaselineRefusal as exc:
                self._log("error", "Planning against the node's observed state refused the run:")
                self._refuse(2, exc)

            self._run_step(3, client, [("PKGS", cfg["packages"])], [["nfv_step_packages"]], timeout=1200)
            if cfg["snmp"]:
                self._run_step(4, client, self._snmp_locals(device, ctx), [["nfv_step_snmp"]])
            else:
                self._step_line(4, "skipped", "host_baseline.snmp.enabled is false")
            locals_, calls = self._ad_calls(ctx, plan)
            self._run_step(5, client, locals_, calls, timeout=600)
            calls = self._account_calls(plan)
            if calls:
                events = self._apply(client, [("DRY_RUN", "1" if self._dry else "0")], calls, timeout=300)
                if not self._dry:
                    self._store_tokens(device, events, host)
                status, detail = self._report(6, events)
                self._step_line(6, status, detail)
                if status == "failed" and not self._dry:
                    raise StepFailed(f"{STEPS[5]} failed on the node — see the log; the network step did not run")
            else:
                self._step_line(6, "ok", "host_baseline.service_accounts is empty")
            self._network(device, ctx, plan, client, obs)
        finally:
            for open_client in self._clients:
                try:
                    open_client.close()
                except Exception:
                    pass

        if self._dry:
            self._step_line(8, "unchanged", f"dry run — stays {device.cf.get('provisioning_state')!r}")
            changes = [line for line in self._summary if ": would change" in line]
            return "\n".join([f"{device.name}: DRY RUN — {len(changes)} step(s) would change, nothing written:"]
                             + self._summary)
        if device.cf.get("provisioning_state") == "bm_installed":
            device._custom_field_data["provisioning_state"] = hb.DONE_STATE
            device.validated_save()
            self._step_line(8, "changed", f"bm_installed -> {hb.DONE_STATE}")
        else:
            self._step_line(8, "ok", f"stays {device.cf.get('provisioning_state')!r}")
        return "\n".join([f"{device.name}: host baseline applied:"] + self._summary)


register_jobs(HostBaseline)
