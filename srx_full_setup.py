#!/usr/bin/env python3
"""
Juniper SRX MNHA — Full Setup Script (ICL + multi-LAN + multi-WAN)
====================================================================
Single SSH session per node:

  ├── Pre-check: detect already-configured interfaces → skip them
  ├── Checkpoint saved
  ├── delete chassis high-availability  (clean slate for HA config)
  ├── ICL interface + IP
  ├── MNHA chassis HA (local-id, peer-id, liveness-detection)
  ├── LAN interfaces × 5  (skips any already configured)
  ├── WAN interfaces × 5  (skips any already configured)
  ├── Default static routes per WAN
  ├── Source NAT (optional, --enable-nat)
  ├── commit check
  └── commit

Settings read from srx_mnha.env.
Priority: CLI args  >  srx_mnha.env  >  built-in defaults
"""

import argparse
import getpass
import pathlib
import paramiko
import sys
import time
import textwrap

# ─── BUILT-IN DEFAULTS ────────────────────────────────────────────────────────

DEFAULTS = {
    "n1_host":       "10.0.0.1",
    "n2_host":       "10.0.0.2",
    "username":      "admin",
    "port":          22,
    # ICL
    "icl_interface": "ge-0/0/0",
    "n1_icl_ip":     "10.255.255.1/30",
    "n2_icl_ip":     "10.255.255.2/30",
    # LAN (5 interfaces, comma-separated)
    "lan_ifaces":    "ge-0/0/2,ge-0/0/3,ge-0/0/4,ge-0/0/5,ge-0/0/6",
    "n1_lan_ips":    "10.30.30.1/24,10.31.31.1/24,10.32.32.1/24,10.33.33.1/24,10.34.34.1/24",
    "n2_lan_ips":    "10.30.30.2/24,10.31.31.2/24,10.32.32.2/24,10.33.33.2/24,10.34.34.2/24",
    # WAN (5 interfaces, comma-separated)
    "wan_ifaces":    "ge-0/0/7,ge-0/0/8,ge-0/0/9,ge-0/0/10,ge-0/0/11",
    "n1_wan_ips":    "10.20.20.1/24,10.21.21.1/24,10.22.22.1/24,10.23.23.1/24,10.24.24.1/24",
    "n2_wan_ips":    "10.20.20.2/24,10.21.21.2/24,10.22.22.2/24,10.23.23.2/24,10.24.24.2/24",
    "wan_gateways":  "10.20.20.254,10.21.21.254,10.22.22.254,10.23.23.254,10.24.24.254",
}

# ─── .ENV LOADER ─────────────────────────────────────────────────────────────


def load_env_file(path: str) -> dict:
    env: dict = {}
    p = pathlib.Path(path)
    if not p.exists():
        return env
    for lineno, raw in enumerate(p.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"  [.env] Skipping malformed line {lineno}: {raw!r}")
            continue
        key, _, value = line.partition("=")
        value = value.split("#")[0].strip().strip('"').strip("'")
        env[key.strip().lower()] = value
    return env


def _csv(val: str) -> list[str]:
    """Split a comma-separated string into a stripped list."""
    return [v.strip() for v in val.split(",") if v.strip()]


def merge_config(env: dict) -> dict:
    merged = dict(DEFAULTS)
    for key in (
        "n1_host", "n2_host",
        "n1_icl_ip", "n2_icl_ip",
        "lan_ifaces", "n1_lan_ips", "n2_lan_ips",
        "wan_ifaces", "n1_wan_ips", "n2_wan_ips",
        "wan_gateways",
    ):
        if key in env:
            merged[key] = env[key]
    for attr in ("n1_icl_iface", "n2_icl_iface", "n1_user", "n2_user"):
        if attr in env:
            merged[attr] = env[attr]
    if "ssh_port"     in env: merged["port"]     = int(env["ssh_port"])
    if "ssh_password" in env: merged["password"] = env["ssh_password"]
    return merged


# ─── ARG PARSER ──────────────────────────────────────────────────────────────


def parse_args(cfg: dict) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Full SRX MNHA setup — ICL + multi-LAN + multi-WAN",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--env-file", default="srx_mnha.env")

    g1 = p.add_argument_group("Node 1 (primary)")
    g1.add_argument("--n1-host",      default=cfg["n1_host"])
    g1.add_argument("--n1-user",      default=cfg.get("n1_user", cfg["username"]))
    g1.add_argument("--n1-icl-ip",    default=cfg["n1_icl_ip"])
    g1.add_argument("--n1-icl-iface", default=cfg.get("n1_icl_iface", cfg["icl_interface"]))
    g1.add_argument("--n1-lan-ips",   default=cfg["n1_lan_ips"],
                    help="Comma-separated LAN IPs for node 1")
    g1.add_argument("--n1-wan-ips",   default=cfg["n1_wan_ips"],
                    help="Comma-separated WAN IPs for node 1")

    g2 = p.add_argument_group("Node 2 (secondary)")
    g2.add_argument("--n2-host",      default=cfg["n2_host"])
    g2.add_argument("--n2-user",      default=cfg.get("n2_user", cfg["username"]))
    g2.add_argument("--n2-icl-ip",    default=cfg["n2_icl_ip"])
    g2.add_argument("--n2-icl-iface", default=cfg.get("n2_icl_iface", cfg["icl_interface"]))
    g2.add_argument("--n2-lan-ips",   default=cfg["n2_lan_ips"],
                    help="Comma-separated LAN IPs for node 2")
    g2.add_argument("--n2-wan-ips",   default=cfg["n2_wan_ips"],
                    help="Comma-separated WAN IPs for node 2")

    gs = p.add_argument_group("Shared options")
    gs.add_argument("--lan-ifaces",   default=cfg["lan_ifaces"],
                    help="Comma-separated LAN interface names (same on both nodes)")
    gs.add_argument("--wan-ifaces",   default=cfg["wan_ifaces"],
                    help="Comma-separated WAN interface names (same on both nodes)")
    gs.add_argument("--wan-gateways", default=cfg["wan_gateways"],
                    help="Comma-separated WAN gateways (one per WAN interface)")
    gs.add_argument("--port",         type=int, default=cfg["port"])
    gs.add_argument("--password",     default=cfg.get("password"))
    gs.add_argument("--enable-nat",   action="store_true",
                    help="Add source NAT rule (trust → untrust masquerade)")
    gs.add_argument("--verify",       action="store_true")

    return p.parse_args()


def build_node_dicts(args: argparse.Namespace) -> tuple[dict, dict]:
    password = args.password
    if not password:
        password = getpass.getpass("SSH password (used for both nodes): ")

    lan_ifaces   = _csv(args.lan_ifaces)
    wan_ifaces   = _csv(args.wan_ifaces)
    wan_gateways = _csv(args.wan_gateways)

    # Pad gateways to match WAN count (repeat last if fewer supplied)
    while len(wan_gateways) < len(wan_ifaces):
        wan_gateways.append(wan_gateways[-1] if wan_gateways else "0.0.0.0")

    node1 = {
        "host":          args.n1_host,
        "username":      args.n1_user,
        "password":      password,
        "port":          args.port,
        "local_id":      1,
        "peer_id":       2,
        "icl_interface": args.n1_icl_iface,
        "icl_ip":        args.n1_icl_ip,
        "lan_ifaces":    lan_ifaces,
        "lan_ips":       _csv(args.n1_lan_ips),
        "wan_ifaces":    wan_ifaces,
        "wan_ips":       _csv(args.n1_wan_ips),
        "wan_gateways":  wan_gateways,
        "enable_nat":    args.enable_nat,
    }
    node2 = {
        "host":          args.n2_host,
        "username":      args.n2_user,
        "password":      password,
        "port":          args.port,
        "local_id":      2,
        "peer_id":       1,
        "icl_interface": args.n2_icl_iface,
        "icl_ip":        args.n2_icl_ip,
        "lan_ifaces":    lan_ifaces,
        "lan_ips":       _csv(args.n2_lan_ips),
        "wan_ifaces":    wan_ifaces,
        "wan_ips":       _csv(args.n2_wan_ips),
        "wan_gateways":  wan_gateways,
        "enable_nat":    args.enable_nat,
    }
    node1["peer_icl_ip"] = node2["icl_ip"].split("/")[0]
    node2["peer_icl_ip"] = node1["icl_ip"].split("/")[0]
    return node1, node2


# ─── SSH HELPERS ─────────────────────────────────────────────────────────────


def ssh_connect(device: dict) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"  [SSH] Connecting to {device['host']}:{device['port']} "
          f"as {device['username']} ...")
    client.connect(
        hostname=device["host"], port=device["port"],
        username=device["username"], password=device["password"],
        look_for_keys=False, allow_agent=False, timeout=15,
    )
    print(f"  [SSH] Connected ✓")
    return client


def wait_for_prompt(channel: paramiko.Channel, expected: str,
                    timeout: float = 10.0, poll: float = 0.2) -> str:
    output, elapsed = "", 0.0
    while elapsed < timeout:
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            if expected in output:
                return output
        time.sleep(poll)
        elapsed += poll
    return output


def get_configured_ifaces(client: paramiko.SSHClient) -> set:
    """
    Return the set of physical interface names that already have an
    IPv4 address configured on the device.
    Uses 'show interfaces terse' — no config mode needed.

    Example line: ge-0/0/2.0   up  up  inet  10.30.30.1/24
    """
    _, stdout, _ = client.exec_command(
        "cli -c 'show interfaces terse | match inet'", timeout=10
    )
    out = stdout.read().decode(errors="replace")
    configured: set = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and "inet" in parts:
            # parts[0] is like "ge-0/0/2.0"
            iface_unit = parts[0]
            iface = iface_unit.split(".")[0]  # strip ".0"
            configured.add(iface)
    return configured


# ─── COMMAND BUILDER ─────────────────────────────────────────────────────────


def build_all_commands(node: dict, skip_ifaces: set) -> list[str]:
    """
    Build the full ordered list of JunOS set commands for one node.
    skip_ifaces: set of interface names already configured — those are skipped.
    """
    icl  = node["icl_interface"]
    pid  = node["peer_id"]
    cmds = []

    # ── 0. Clean slate for chassis HA ─────────────────────────────────────────
    cmds += [
        "delete chassis high-availability",
    ]

    # ── 1. ICL interface ──────────────────────────────────────────────────────
    if icl in skip_ifaces:
        print(f"    [skip] ICL {icl} already configured — skipping interface IP")
    else:
        cmds += [
            f"set interfaces {icl} description \"MNHA-ICL-to-peer\"",
            f"set interfaces {icl} unit 0 family inet address {node['icl_ip']}",
        ]

    # ── 2. MNHA chassis high-availability ─────────────────────────────────────
    cmds += [
        f"set chassis high-availability local-id {node['local_id']}",
        f"set chassis high-availability peer-id {pid} peer-ip {node['peer_icl_ip']}",
        f"set chassis high-availability peer-id {pid} interface {icl}",
        f"set chassis high-availability peer-id {pid} liveness-detection minimum-interval 1000",
        f"set chassis high-availability peer-id {pid} liveness-detection multiplier 3",
    ]

    # ── 3. LAN interfaces (5 × skip-aware) ────────────────────────────────────
    configured_lans = []   # track which LANs we actually configure
    for idx, (iface, ip) in enumerate(zip(node["lan_ifaces"], node["lan_ips"]), 1):
        if iface in skip_ifaces:
            print(f"    [skip] LAN {idx}: {iface} ({ip}) — already configured")
            configured_lans.append(iface)   # still register in SRG even if IP exists
            continue
        zone_name = "trust"
        cmds += [
            f"set interfaces {iface} description \"LAN{idx}-NODE{node['local_id']}\"",
            f"set interfaces {iface} unit 0 family inet address {ip}",
            f"set security zones security-zone {zone_name} interfaces {iface}.0",
            f"set security zones security-zone {zone_name} interfaces {iface}.0 "
            f"host-inbound-traffic system-services ping",
            f"set security zones security-zone {zone_name} interfaces {iface}.0 "
            f"host-inbound-traffic system-services ssh",
        ]
        configured_lans.append(iface)

    # ── 4. WAN interfaces (5 × skip-aware) + static routes ───────────────────
    configured_wans = []   # track which WANs we actually configure
    for idx, (iface, ip, gw) in enumerate(
            zip(node["wan_ifaces"], node["wan_ips"], node["wan_gateways"]), 1):
        if iface in skip_ifaces:
            print(f"    [skip] WAN {idx}: {iface} ({ip}) — already configured")
            configured_wans.append(iface)
            continue
        zone_name = "untrust"
        cmds += [
            f"set interfaces {iface} description \"WAN{idx}-NODE{node['local_id']}\"",
            f"set interfaces {iface} unit 0 family inet address {ip}",
            f"set security zones security-zone {zone_name} interfaces {iface}.0",
            f"set security zones security-zone {zone_name} interfaces {iface}.0 "
            f"host-inbound-traffic system-services ping",
        ]
        if idx == 1:
            cmds.append(f"set routing-options static route 0.0.0.0/0 next-hop {gw}")
        else:
            wan_subnet = ip.rsplit(".", 1)[0] + ".0/" + ip.split("/")[1]
            cmds.append(f"set routing-options static route {wan_subnet} next-hop {gw}")
        configured_wans.append(iface)

    # ── 5. SRG (Service Redundancy Group) — register LAN and WAN interfaces ───
    # MNHA requires interfaces to be explicitly listed as lan-interface or
    # wan-interface inside the SRG before virtual IPs can be assigned to them.
    # Without this, MNHA raises:
    #   "virtual IP for interface X.0 is not a configured LAN or WAN interface"
    srg = 1
    for iface in configured_lans:
        cmds.append(
            f"set chassis high-availability service-redundancy-group {srg} "
            f"lan-interface {iface}"
        )
    for iface in configured_wans:
        cmds.append(
            f"set chassis high-availability service-redundancy-group {srg} "
            f"wan-interface {iface}"
        )

    # ── 5. Optional source NAT ────────────────────────────────────────────────
    if node.get("enable_nat"):
        cmds += [
            "set security nat source rule-set TRUST-TO-WAN from zone trust",
            "set security nat source rule-set TRUST-TO-WAN to zone untrust",
            "set security nat source rule-set TRUST-TO-WAN rule NAT-RULE "
            "match source-address 0.0.0.0/0",
            "set security nat source rule-set TRUST-TO-WAN rule NAT-RULE "
            "then source-nat interface",
        ]

    return cmds


# ─── FULL CONFIG RUNNER ───────────────────────────────────────────────────────


def run_full_config(device: dict) -> str:
    checkpoint_file = "/var/tmp/pre_full_setup_checkpoint.conf"
    full_output = ""

    client = ssh_connect(device)

    # ── Pre-check: detect already-configured interfaces (before config mode) ──
    print("    [pre] Checking existing interface configuration ...")
    skip_ifaces = get_configured_ifaces(client)
    if skip_ifaces:
        print(f"    [pre] Already configured: {', '.join(sorted(skip_ifaces))}")
    else:
        print("    [pre] No existing interface IPs found — configuring all")

    # Build commands now that we know what to skip
    config_commands = build_all_commands(device, skip_ifaces)
    print(f"    [pre] {len(config_commands)} commands queued after skip checks")

    channel = client.invoke_shell(term="vt100", width=512, height=64)

    # ── 1. Enter Junos CLI ────────────────────────────────────────────────────
    print("    [1] Entering Junos CLI ...")
    wait_for_prompt(channel, expected=">", timeout=10)
    channel.send("cli\n")
    out = wait_for_prompt(channel, expected=">", timeout=10)
    full_output += out
    print("        CLI prompt (>) confirmed ✓")

    # ── 2. Enter config mode ──────────────────────────────────────────────────
    print("    [2] Entering configuration mode ...")
    channel.send("configure\n")
    out = wait_for_prompt(channel, expected="[edit]", timeout=10)
    full_output += out
    if "[edit]" not in out:
        channel.close(); client.close()
        raise RuntimeError(f"Failed to enter config mode on {device['host']}")
    print("        [edit] confirmed ✓")

    # ── 3. Save checkpoint ────────────────────────────────────────────────────
    print(f"    [3] Saving checkpoint → {checkpoint_file} ...")
    channel.send(f"save {checkpoint_file}\n")
    out = wait_for_prompt(channel, expected="[edit]", timeout=10)
    full_output += out
    print(f"        Checkpoint saved ✓")
    print(f"        Restore: load override {checkpoint_file} → commit")

    # ── 4. Push all commands ──────────────────────────────────────────────────
    print(f"    [4] Pushing {len(config_commands)} commands ...")
    errors_found = False
    for cmd in config_commands:
        channel.send(cmd + "\n")
        out = wait_for_prompt(channel, expected="[edit]", timeout=5)
        full_output += out
        for line in out.splitlines():
            if any(kw in line.lower() for kw in ("syntax error", "invalid", "error:")):
                print(f"        [!] {line.strip()}")
                errors_found = True

    # ── 5. Commit check ───────────────────────────────────────────────────────
    if errors_found:
        print("\n    [5] SKIPPING commit — errors in commands above.")
        channel.send("rollback 0\n")
        wait_for_prompt(channel, expected="[edit]", timeout=5)
        print(f"        Rolled back. Restore: load override {checkpoint_file} → commit")
    else:
        print("    [5] Running commit check ...")
        channel.send("commit check\n")
        out = wait_for_prompt(channel, expected="[edit]", timeout=20)
        full_output += out

        check_ok  = "configuration check succeeds" in out.lower()
        check_err = "error" in out.lower()

        if check_err or not check_ok:
            print("        [ERROR] Commit check failed — output:")
            print("        " + "─" * 52)
            for line in out.splitlines():
                if line.strip():
                    print(f"          {line}")
            print("        " + "─" * 52)
            channel.send("rollback 0\n")
            wait_for_prompt(channel, expected="[edit]", timeout=5)
        else:
            print("        Commit check passed ✓")
            print("    [6] Committing ...")
            channel.send("commit\n")
            out = wait_for_prompt(channel, expected="commit complete", timeout=30)
            full_output += out

            if "commit complete" in out.lower():
                print("        Commit complete ✓  — full config is live")
                print(f"        Checkpoint: {checkpoint_file}")
            elif "error" in out.lower():
                print("        [ERROR] Commit failed — restoring checkpoint ...")
                channel.send(f"load override {checkpoint_file}\n")
                wait_for_prompt(channel, expected="[edit]", timeout=15)
                channel.send("commit\n")
                out2 = wait_for_prompt(channel, expected="commit complete", timeout=30)
                full_output += out2
                print("        Restored checkpoint ✓" if "commit complete" in out2.lower()
                      else "        [WARNING] Restore unclear — check: show system commit")
            else:
                out2 = wait_for_prompt(channel, expected="[edit]", timeout=15)
                full_output += out2
                print("        Commit complete ✓" if "commit complete" in (out + out2).lower()
                      else "        [WARNING] Commit status unclear — run: show system commit")

    # ── 7. Exit ───────────────────────────────────────────────────────────────
    print("    [7] Exiting configuration mode ...")
    channel.send("exit\n")
    wait_for_prompt(channel, expected=">", timeout=5)
    channel.send("exit\n")
    time.sleep(1)
    channel.close()
    client.close()
    return full_output


# ─── VERIFY ──────────────────────────────────────────────────────────────────


def verify_node(device: dict, node_label: str) -> None:
    print(f"\n  Verifying {node_label} ({device['host']}) ...")
    client = ssh_connect(device)
    checks = [
        "cli -c 'show interfaces terse'",
        "cli -c 'show route'",
        "cli -c 'show chassis high-availability'",
        "cli -c 'show security zones'",
    ]
    for cmd in checks:
        _, stdout, _ = client.exec_command(cmd, timeout=10)
        label = cmd.split("-c")[1].strip().strip("'")
        print(f"\n  --- {label} ---")
        print(textwrap.indent(stdout.read().decode(), "  "))
    client.close()


# ─── CONFIGURE NODE ──────────────────────────────────────────────────────────


def configure_node(node: dict, node_label: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  Full setup — {node_label}  ({node['host']})")
    print(f"{'=' * 60}")

    nat_note = "  (+ source NAT)" if node.get("enable_nat") else ""
    confirm = input(
        f"\n  Apply full config to {node_label} ({node['host']})?{nat_note} [y/N]: "
    ).strip().lower()
    if confirm != "y":
        print(f"  Skipped {node_label}.")
        return

    print(f"\n  Applying configuration ...")
    try:
        output = run_full_config(node)
        print(f"\n  --- Raw SSH output ({node_label}) ---")
        print(textwrap.indent(output, "  "))

        error_lines = [
            ln for ln in output.splitlines()
            if "syntax error" in ln.lower()
            or ln.strip().lower().startswith("error:")
            or "invalid input" in ln.lower()
        ]
        if error_lines:
            print(f"\n  [WARNING] Errors on {node_label}:")
            for ln in error_lines:
                print(f"    {ln.strip()}")
        else:
            print(f"\n  [OK] Full configuration committed on {node_label}.")

    except Exception as exc:
        print(f"\n  [ERROR] {node_label}: {exc}", file=sys.stderr)
        raise


# ─── MAIN ────────────────────────────────────────────────────────────────────


def main() -> None:
    env_file = "srx_mnha.env"
    for i, arg in enumerate(sys.argv):
        if arg == "--env-file" and i + 1 < len(sys.argv):
            env_file = sys.argv[i + 1]

    env_data = load_env_file(env_file)
    print(f"  [.env] Loaded {len(env_data)} settings from '{env_file}'"
          if env_data else f"  [.env] '{env_file}' not found — using defaults")

    cfg            = merge_config(env_data)
    args           = parse_args(cfg)
    NODE_1, NODE_2 = build_node_dicts(args)
    nat_note       = "  (+ source NAT)" if args.enable_nat else ""

    lan_ifaces = _csv(args.lan_ifaces)
    wan_ifaces = _csv(args.wan_ifaces)

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  Juniper SRX MNHA — Full Setup (ICL + {len(lan_ifaces)} LAN + {len(wan_ifaces)} WAN)        ║
╚══════════════════════════════════════════════════════════════╝

 Node 1  :  {NODE_1['host']}
   ICL   :  {NODE_1['icl_interface']}  →  {NODE_1['icl_ip']}
   LANs  :  {", ".join(f"{i}={ip}" for i, ip in zip(lan_ifaces, NODE_1['lan_ips']))}
   WANs  :  {", ".join(f"{i}={ip}" for i, ip in zip(wan_ifaces, NODE_1['wan_ips']))}{nat_note}

 Node 2  :  {NODE_2['host']}
   ICL   :  {NODE_2['icl_interface']}  →  {NODE_2['icl_ip']}
   LANs  :  {", ".join(f"{i}={ip}" for i, ip in zip(lan_ifaces, NODE_2['lan_ips']))}
   WANs  :  {", ".join(f"{i}={ip}" for i, ip in zip(wan_ifaces, NODE_2['wan_ips']))}{nat_note}

 Note: interfaces already configured on the device will be skipped.
""")

    configure_node(NODE_1, "Node-1 (primary)")
    configure_node(NODE_2, "Node-2 (secondary)")

    if args.verify or input(
        "\nRun verification (show interfaces/route/ha/zones)? [y/N]: "
    ).strip().lower() == "y":
        verify_node(NODE_1, "Node-1")
        verify_node(NODE_2, "Node-2")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
